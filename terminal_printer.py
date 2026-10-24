
import traceback
from math import floor


def print_exception(e : Exception):
    type_ref = type(e).__name__
    type_ref_str = red_str(type_ref)
    e_message = red_str(str(e).strip())
    print(f"Application threw exception of type {type_ref_str}")
    print(f"Specific Error Message: {e_message}")
    print(underline_str("Traceback (most recent call last):"))
    for num, info in enumerate(traceback.format_exception(e)):
        if num < 2 or num > 3: continue
        exec_msg= traceback.format_exception(type(e), e, e.__traceback__)[num-1]
        print(exec_msg)
    quit(1)


def red_str(s : str): return "\033[91m{}\033[00m".format(s).strip()
def green_str(s : str): return "\033[92m{}\033[00m".format(s).strip()
def yellow_str(s: str): return "\033[93m{}\033[00m".format(s)
def underline_str(s: str): return "\x1b[4m{}\x1b[24m".format(s)

def light_purple_str(s: str): return "\033[94m{}\033[00m".format(s)
def purple_str(s: str): return "\033[95m{}\033[00m".format(s)
def cyan_str(s: str): return "\033[96m{}\033[00m".format(s)
def light_grey_str(s: str): return "\033[97m{}\033[00m".format(s)


def print_no_changes_message():
    bounds = "================================================"
    print(bounds)
    no_chg = underline_str("No Changes present in repository")
    l_pad_amnt = floor((len(bounds) - len(no_chg)) / 2) + 1
    print(l_pad(no_chg, l_pad_amnt))
    exit_str = yellow_str("Exiting...")
    l_pad_amnt = floor((len(bounds) - len(exit_str)) / 2) + 1
    print(l_pad(exit_str, l_pad_amnt))
    print(bounds)
    quit(0)

def l_pad(s: str, length: int) -> str:
    if length < 0:
        return s
    else:
        return (" " * length) + s