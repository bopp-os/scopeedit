import os
import json
import argparse
import curses
import shlex
import shutil
import subprocess
import sys
import textwrap
import urllib.error
import urllib.parse
import urllib.request


KEYS_UP = (curses.KEY_UP, ord("k"))
KEYS_DOWN = (curses.KEY_DOWN, ord("j"))
KEYS_ENTER = (curses.KEY_ENTER, ord("\n"), ord("\r"))


class Picker:
    """Single-choice terminal menu. start() returns (option, index); a quit key returns (None, -1)."""

    def __init__(self, options, title=None, indicator="*", default_index=0, quit_keys=None):
        if not options:
            raise ValueError("options should not be an empty list")
        self.options = options
        self.title = title
        self.indicator = indicator
        self.index = max(0, min(default_index, len(options) - 1))
        self.quit_keys = tuple(quit_keys or ())

    def lines(self, width):
        """Title rows (wrapped to the terminal) and a blank row, then the options."""
        title = []
        if self.title:
            for line in self.title.split("\n"):
                title += textwrap.wrap(line, max(1, width - 1)) or [""]
            title.append("")
        pad = " " * len(self.indicator)
        options = [f"{self.indicator if i == self.index else pad} {option}" for i, option in enumerate(self.options)]
        return title, options

    def draw(self, screen):
        screen.erase()
        height, width = screen.getmaxyx()
        title, options = self.lines(width)
        # Scroll so the current option stays visible below the title
        rows = max(1, height - len(title) - 1)
        first = max(0, self.index - rows + 1)
        y = 0
        for line in title + options[first:first + rows]:
            if y >= height:
                break
            screen.addnstr(y, 0, line, max(0, width - 1))
            y += 1
        screen.refresh()

    def run(self, screen):
        try:
            curses.curs_set(0)
        except curses.error:
            pass  # some terminals can't hide the cursor
        try:
            # Keep the terminal's own colours, including a transparent background
            curses.start_color()
            curses.use_default_colors()
        except curses.error:
            pass  # terminals without colour support
        while True:
            self.draw(screen)
            key = screen.getch()
            if key in KEYS_UP:
                self.index = (self.index - 1) % len(self.options)
            elif key in KEYS_DOWN:
                self.index = (self.index + 1) % len(self.options)
            elif key in KEYS_ENTER:
                return self.options[self.index], self.index
            elif key in self.quit_keys:
                return None, -1

    def start(self):
        return curses.wrapper(self.run)


def pick(options, title=None, indicator="*", default_index=0, quit_keys=None, multiselect=False):
    if multiselect:
        raise NotImplementedError("multiselect is not supported")
    return Picker(options, title, indicator, default_index, quit_keys).start()

HOME = os.environ.get("HOME")
CONFIG_DIR = os.path.join(HOME, ".config/scopebuddy")
APPID_DIR = os.path.join(CONFIG_DIR, "AppID")
GLOBAL_CONFIG = os.path.join(CONFIG_DIR, "scb.conf")
DATABASE_FILE = os.path.join(CONFIG_DIR, "scopeedit.db")
SETTINGS_FILE = os.path.join(CONFIG_DIR, "scopeedit.json")
DEFAULT_SETTINGS = {"page_size": "auto", "editor": "", "show_appid": True, "show_hidden": False, "show_legend": True}
SETTINGS = dict(DEFAULT_SETTINGS)

def debug(message):
    """Prints debug messages to stderr if the DEBUG environment variable is set."""
    if os.environ.get("DEBUG"):
        print(f"DEBUG: {message}", file=sys.stderr)

def list_dir(path):
    """Files in path, or [] when it doesn't exist yet (no config has been saved)."""
    try:
        return os.listdir(path)
    except FileNotFoundError:
        return []

def get_global_files():
    return sorted(file for file in list_dir(CONFIG_DIR) if file.endswith(".conf"))

def ensure_config_dirs():
    """Creates the ScopeBuddy config folders. Called just before a config is written."""
    os.makedirs(APPID_DIR, exist_ok=True)

def load_settings():
    if os.path.exists(SETTINGS_FILE):
        with open(SETTINGS_FILE, "r") as f:
            SETTINGS.update(json.load(f))
        debug(f"Loaded settings: {SETTINGS}")

def save_settings():
    if not os.path.isdir(CONFIG_DIR):
        debug("No config folder yet; keeping settings in memory")
        return
    with open(SETTINGS_FILE, "w") as f:
        json.dump(SETTINGS, f, indent=2)
    debug(f"Saved settings: {SETTINGS}")

def get_editor():
    return SETTINGS["editor"] or os.environ.get("EDITOR", "nano")  # default editor to nano

def get_game_name(app_id):
    """Gets the game name from the database or the Steam API."""
    debug(f"get_game_name called with app_id: {app_id}")

    # Check database first
    if os.path.exists(DATABASE_FILE):
        with open(DATABASE_FILE, "r") as f:
            for line in f:
                db_app_id, db_name = line.strip().split(" ", 1)
                if db_app_id == app_id:
                    debug(f"Found game name in database: {db_name}")
                    return db_name

    # If not in database, use Steam API
    debug(f"Fetching game name from Steam API for {app_id}")
    try:
        api_url = f"https://store.steampowered.com/api/appdetails?appids={app_id}"
        with urllib.request.urlopen(api_url, timeout=10) as response:
            api_data = json.load(response)

        # Steam sometimes keys the reply by a different AppID, so take the only entry
        app_data = next(iter(api_data.values()))
        if not app_data['success']:
            debug("Error: steam api returned success false")
            return f"(Unknown Game - AppID: {app_id})"
        name = app_data["data"]["name"]
        debug(f"Found game name from API: {name}")

        # Add to database, once the config folder exists
        if os.path.isdir(CONFIG_DIR):
            with open(DATABASE_FILE, "a") as f:
                f.write(f"{app_id} {name}\n")
                debug(f"Added to database: {app_id} {name}")
        return name

    except (urllib.error.URLError, OSError) as e:
        debug(f"Error fetching game name from API: {e}")
        return f"(Unknown Game - AppID: {app_id})"
    except (json.JSONDecodeError, KeyError) as e:
        debug(f"Error parsing game name: {e}")
        return f"(Unknown Game - AppID: {app_id})"

def search_game_by_name(game_name):
    """Searches for a game on the Steam Store and returns a list of possible AppIDs and names."""
    debug(f"search_game_by_name called with game_name: {game_name}")
    try:
        search_url = f"https://store.steampowered.com/api/storesearch/?term={urllib.parse.quote(game_name)}&l=english&cc=us"  # search with English names
        with urllib.request.urlopen(search_url, timeout=10) as response:
            search_data = json.load(response)
        results = []
        for item in search_data["items"]:
            results.append((item["id"], item["name"]))
        return results
    except (urllib.error.URLError, OSError) as e:
        debug(f"Error searching for game: {e}")
        return []
    except (json.JSONDecodeError, KeyError) as e:
        debug(f"Error parsing game search results: {e}")
        return []

def get_game_configs():
    """Returns (label, path) for each per-game config, sorted by game name."""
    configs = []
    for file in list_dir(APPID_DIR):
        hidden = file.endswith(".conf.disabled")
        if file.endswith(".conf") or (hidden and SETTINGS["show_hidden"]):
            debug(f"Found potential config file: {file}")
            app_id = file.split(".")[0]
            label = get_game_name(app_id)
            if SETTINGS["show_appid"]:
                label += f" (AppID: {app_id})"
            if hidden:
                label += " [disabled]"
            configs.append((label, os.path.join(APPID_DIR, file)))
    return sorted(configs, key=lambda c: c[0].lower())

def get_page_size(fixed_rows):
    if SETTINGS["page_size"] != "auto":
        return SETTINGS["page_size"]
    # Picker draws the title (up to 2 rows when wrapped) and a blank line above the options; keep 1 spare row
    return max(1, shutil.get_terminal_size().lines - 4 - fixed_rows)

def pick_game(title, top_options, bottom_options, on_right=None):
    """Shows configs one page at a time. Returns the selected config path or option string.
    If on_right is given, pressing Right on a game calls on_right(path) and redraws the same page."""
    page = 0
    cursor = 0
    while True:
        # Reloaded each time so resizes and on_right changes show up; 2 rows reserved for Next/Previous
        configs = get_game_configs()
        page_size = get_page_size(len(top_options) + len(bottom_options) + 2)
        total = max(1, (len(configs) + page_size - 1) // page_size)
        page = min(page, total - 1)
        page_configs = configs[page * page_size:(page + 1) * page_size]
        nav = []
        if page < total - 1:
            nav.append("Next Page")
        if page > 0:
            nav.append("Previous Page")
        options = top_options + [label for label, _ in page_configs] + nav + bottom_options
        page_title = f"{title} (Page {page + 1}/{total}{', Right: actions' if on_right else ''})"
        picker = Picker(options, page_title, indicator='->', default_index=min(cursor, len(options) - 1),
                        quit_keys=(curses.KEY_RIGHT,) if on_right else None)
        selected = picker.start()
        index = picker.index - len(top_options)
        if selected[1] == -1:  # Right pressed
            if 0 <= index < len(page_configs):
                on_right(page_configs[index][1])
            cursor = picker.index
            continue
        cursor = 0
        if 0 <= index < len(page_configs):
            return page_configs[index][1]
        if selected[0] == "Next Page":
            page += 1
        elif selected[0] == "Previous Page":
            page -= 1
        else:
            return selected[0]

def open_in_editor(config_file):
    try:
        debug(f"Opening {config_file} with {get_editor()}")
        subprocess.run(shlex.split(get_editor()) + [config_file], check=True)
    except subprocess.CalledProcessError as e:
        print(f"Error opening file with {get_editor()}: {e}")

def toggle_disabled(config_file):
    """Disables a config by renaming it to .disabled, or enables it again. Returns the new path, or None on error."""
    name = get_game_name(os.path.basename(config_file).split(".")[0])
    if config_file.endswith(".disabled"):
        new_file = config_file[:-9]  # Remove .disabled
        action = "enabled"
    else:
        new_file = config_file + ".disabled"
        action = "disabled"
    try:
        os.rename(config_file, new_file)
        print(f"Configuration {action}: {name}")
        debug(f"Renamed {config_file} to {new_file}")
        return new_file
    except OSError as e:
        print(f"Error renaming configuration: {e}")
        return None

def game_actions(config_file):
    """Side menu for a single game, opened with Right. Left or Back returns."""
    name = get_game_name(os.path.basename(config_file).split(".")[0])
    options = ["Edit", "Enable" if config_file.endswith(".disabled") else "Disable", "Back"]
    selected = pick(options, f"{name}: (Left: back)", indicator='->', quit_keys=(curses.KEY_LEFT,))
    if selected[0] == "Edit":
        open_in_editor(config_file)
    elif selected[0] in ("Enable", "Disable"):
        toggle_disabled(config_file)

def remove_config():
    """Hides a configuration file by renaming it."""
    debug("remove_config called")

    if not get_game_configs():
        print("No configurations to remove.")
        return

    title = "Select a configuration to remove (disable):"
    if SETTINGS["show_hidden"]:
        title = "Select a configuration to remove (disable) or restore:"
    selected_file = pick_game(title, [], ["Back"])

    if selected_file != "Back":
        toggle_disabled(selected_file)

def options_menu():
    """Edits and saves scopeedit settings."""
    debug("options_menu called")
    while True:
        options = [
            f"Games per page: {'Auto (fit window)' if SETTINGS['page_size'] == 'auto' else SETTINGS['page_size']}",
            f"Editor: {SETTINGS['editor'] or '$EDITOR (' + get_editor() + ')'}",
            f"Show AppID in list: {'On' if SETTINGS['show_appid'] else 'Off'}",
            f"Show disabled configs: {'On' if SETTINGS['show_hidden'] else 'Off'}",
            "Back",
        ]
        selected = pick(options, "Options:", indicator='->', multiselect=False)
        index = selected[1]

        if index == 0:
            value = input("Games per page (number, or blank for auto): ").strip()
            if not value:
                SETTINGS["page_size"] = "auto"
            elif value.isdigit() and int(value) > 0:
                SETTINGS["page_size"] = int(value)
            else:
                print("Invalid number.")
                continue
        elif index == 1:
            SETTINGS["editor"] = input("Editor command (blank to use $EDITOR): ").strip()
        elif index == 2:
            SETTINGS["show_appid"] = not SETTINGS["show_appid"]
        elif index == 3:
            SETTINGS["show_hidden"] = not SETTINGS["show_hidden"]
        else:
            return
        save_settings()

def edit_global():
    debug("edit_global called")
    global_files = get_global_files()
    if not global_files:
        print("No Global files found.")
        return False
    GLOBAL_FILES_LIST = global_files + ["Back"]

    title = "Select a Global Configuration to edit:"
    selected = pick(GLOBAL_FILES_LIST, title, indicator='->', multiselect=False)
    choice = selected[0]

    if choice == "Back":
        return False
    else:
        file = os.path.join(CONFIG_DIR, choice)

        if not os.path.exists(file):
            print(f"Error: Config file not found: {file}")
            os.sys.exit(1)
        
        #Open the editor with Popen
        try:
            debug(f"Opening {file} with {get_editor()}")
            subprocess.run(shlex.split(get_editor()) + [file], check=True)
        except subprocess.CalledProcessError as e:
            print(f"Error opening file with {get_editor()}: {e}")
            os.sys.exit(1)

def list_configs():
    """Lists available configurations and handles user interaction."""
    debug("list_configs called")

    title = "Select a ScopeBuddy configuration to edit:"
    choice = pick_game(title, ["Global Configuration"],
                       ["", "Create New Configuration", "Remove Configuration", "Options", "Quit"],
                       on_right=game_actions)

    if choice == "Quit":
        print("Exiting...")
        return False
    elif choice == "":
        return True
    elif choice == "Create New Configuration":
        create_new_config()
        return True
    elif choice == "Remove Configuration":
        remove_config()
        return True
    elif choice == "Options":
        options_menu()
        return True
    elif choice == "Global Configuration":
        edit_global()
        return True
    else:
        selected_config = choice
        debug(f"Selected config file: {selected_config}")

        if not os.path.exists(selected_config):
            print(f"Error: Config file not found: {selected_config}")
            return True

        open_in_editor(selected_config)
        return True

def create_new_config():
    """Creates a new configuration file."""
    game_name = input("Enter the name of the game: ")
    if not game_name:
        print("Game name cannot be empty.")
        return True

    search_results = search_game_by_name(game_name)

    if not search_results:
        print(f"No games found matching '{game_name}'.")
        app_id = input("Please enter the AppID manually or press enter to quit: ")
        if not app_id:
            return False
    elif len(search_results) == 1:
        app_id = search_results[0][0]
        print(f"Found: {search_results[0][1]} ({app_id})")
    else:
        print("Multiple games found:")
        options = [f"{name} ({app_id})" for app_id, name in search_results]
        options.append("Enter AppID Manually")
        title = "Select a Game"
        selected = pick(options, title, indicator='->', multiselect=False)
        if selected[0] == "Enter AppID Manually":
            app_id = input("Please enter the AppID manually or press enter to quit: ")
            if not app_id:
                return False
        else:
            app_id = selected[0].split(" ")[-1].strip("()")

    if app_id:
        if not app_id.isdigit():
            print("Invalid AppID. Please use numbers")
            return True

        new_config_file = os.path.join(APPID_DIR, f"{app_id}.conf")
        disabled_config_file = new_config_file + ".disabled"

        if os.path.exists(new_config_file):
            print(f"Error: Configuration file for AppID {app_id} already exists.")
            return True
        
        if os.path.exists(disabled_config_file):
            print(f"Found disabled configuration. Restoring...")
            try:
                os.rename(disabled_config_file, new_config_file)
                debug(f"Restored {disabled_config_file} to {new_config_file}")
            except OSError as e:
                print(f"Error restoring configuration: {e}")
                return True
            
            try:
                debug(f"Opening {new_config_file} with {get_editor()}")
                subprocess.run(shlex.split(get_editor()) + [new_config_file], check=True)
            except subprocess.CalledProcessError as e:
                print(f"Error opening file with {get_editor()}: {e}")
            return False

        game_name = get_game_name(app_id)

        ensure_config_dirs()
        with open(new_config_file, "w") as f:
            f.write(f"# {game_name}\n")

        print(f"Created new configuration: {new_config_file}")
        # open with Popen
        try:
            debug(f"Opening {new_config_file} with {get_editor()}")
            subprocess.run(shlex.split(get_editor()) + [new_config_file], check=True)
        except subprocess.CalledProcessError as e:
            print(f"Error opening file with {get_editor()}: {e}")
            return True

        return False

def main():
    parser = argparse.ArgumentParser(description="ScopeBuddy configuration editor")
    parser.add_argument("-d", "--debug", action="store_true", help="Enable debug output")
    args = parser.parse_args()
    if args.debug:
        os.environ["DEBUG"] = "1"
    load_settings()
    while list_configs():
        pass
