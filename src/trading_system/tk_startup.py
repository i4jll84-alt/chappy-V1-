"""Create desktop windows with bounded retry for transient Windows Tcl file reads."""

from time import sleep


def create_window(tk, stop_reason=lambda: None):
    for attempt in range(3):
        if stop_reason():
            return None
        try:
            return tk.Tk()
        except tk.TclError as exc:
            if stop_reason():
                return None
            # Windows may briefly fail to read an installed Tcl/Tk startup script.
            if attempt == 2 or not str(exc).startswith(
                ("Can't find a usable init.tcl", "Can't find a usable tk.tcl")
            ):
                raise
            sleep(0.05)
