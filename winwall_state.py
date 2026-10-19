"""Keep wallpaper state consistent across desktop, startup and packaged launches."""
import json
import os
import shutil


def data_dir(profile=None, legacy=None):
    profile = profile or os.path.expanduser("~")
    legacy = legacy or os.path.join(os.environ.get("LOCALAPPDATA", profile), "WinWall")
    target = os.path.join(profile, ".winwall")
    os.makedirs(target, exist_ok=True)
    settings = os.path.join(target, "settings.json")
    if os.path.exists(settings):
        return target
    for name in ("library", "thumbnails"):
        source, destination = os.path.join(legacy, name), os.path.join(target, name)
        if os.path.isdir(source) and not os.path.exists(destination):
            shutil.copytree(source, destination)
    old_settings = os.path.join(legacy, "settings.json")
    if os.path.isfile(old_settings):
        try:
            with open(old_settings, encoding="utf-8-sig") as handle:
                values = json.load(handle)
            if not isinstance(values, dict):
                return target
        except (OSError, ValueError):
            return target
        last = values.get("last_wallpaper", "")
        if last:
            try:
                relative = os.path.relpath(last, legacy)
                if relative != os.pardir and not relative.startswith(os.pardir + os.sep):
                    migrated = os.path.join(target, relative)
                    if os.path.isfile(migrated):
                        values["last_wallpaper"] = migrated
            except ValueError:
                pass
        with open(settings, "w", encoding="utf-8") as handle:
            json.dump(values, handle, indent=2)
    return target
