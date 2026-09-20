"""Terminal picker with arrow keys and a numbered-input fallback."""

import os
import shutil
import sys
import textwrap


def _numbered_pick(options: list[tuple[str, str]]) -> str | None:
    for number, (_, label) in enumerate(options, 1):
        print(f"  {number}. {label}")
    while True:
        try:
            value = input(
                f"Select account [1-{len(options)}], or q to cancel: "
            ).strip()
        except (EOFError, KeyboardInterrupt):
            print("\nSelection cancelled.")
            return None
        if value.casefold() in ("q", "quit", "cancel", ""):
            print("Selection cancelled.")
            return None
        if value.isascii() and value.isdigit() and 0 < int(value) <= len(options):
            return options[int(value) - 1][0]
        print("Enter one of the numbers shown above.")


def pick(options: list[tuple[str, str]], *, selected: str | None = None) -> str | None:
    if not options:
        return None
    if (
        not (sys.stdin.isatty() and sys.stdout.isatty())
        or os.environ.get("TERM") == "dumb"
    ):
        return _numbered_pick(options)

    if os.name == "nt":
        try:
            import ctypes
            import msvcrt

            kernel32 = ctypes.windll.kernel32
            handle = kernel32.GetStdHandle(-11)  # STD_OUTPUT_HANDLE
            mode = ctypes.c_ulong()
            if kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
                kernel32.SetConsoleMode(
                    handle, mode.value | 0x0004
                )  # ENABLE_VIRTUAL_TERMINAL_PROCESSING
        except Exception:
            return _numbered_pick(options)
    elif os.name != "posix":
        return _numbered_pick(options)

    index = next((i for i, (key, _) in enumerate(options) if key == selected), 0)
    drawn = 0
    chosen = None

    def draw() -> None:
        nonlocal drawn
        size = shutil.get_terminal_size()
        width = max(1, size.columns - 1)
        available = max(1, size.lines - 6)
        rows = []
        for i, (_, label) in enumerate(options):
            mark = ">" if i == index else " "
            rows.append(
                textwrap.wrap(
                    f" {mark} {i + 1}. {label}",
                    width=width,
                    subsequent_indent="    " if width > 4 else "",
                    break_on_hyphens=False,
                )
            )
        start = end = index
        used = len(rows[index])
        while start > 0 and used + len(rows[start - 1]) <= available // 2:
            start -= 1
            used += len(rows[start])
        while end + 1 < len(rows) and used + len(rows[end + 1]) <= available:
            end += 1
            used += len(rows[end])
        while start > 0 and used + len(rows[start - 1]) <= available:
            start -= 1
            used += len(rows[start])
        if drawn:
            sys.stdout.write(f"\x1b[{drawn}A\r")
        lines = ["Select an account", ""]
        for row in rows[start : end + 1]:
            lines.extend(row[:available])
        lines += ["", "Up/Down or j/k | Enter select | Esc cancel"]
        for line in lines:
            sys.stdout.write("\x1b[2K" + line[:width] + "\r\n")
        for _ in range(max(0, drawn - len(lines))):
            sys.stdout.write("\x1b[2K\r\n")
        if drawn > len(lines):
            sys.stdout.write(f"\x1b[{drawn - len(lines)}A")
        drawn = len(lines)
        sys.stdout.flush()

    if os.name == "nt":
        import msvcrt

        try:
            sys.stdout.write("\x1b[?25l")
            draw()
            while True:
                ch = msvcrt.getwch()
                if ch in ("\x00", "\xe0"):
                    code = msvcrt.getwch()
                    key = "k" if code == "H" else ("j" if code == "P" else "ignore")
                elif ch in ("\r", "\n"):
                    key = "\n"
                elif ch in ("\x1b", "q", "Q", "\x03", "\x04"):
                    key = "q"
                elif ch in ("j", "k"):
                    key = ch
                else:
                    key = "ignore"

                if key in ("", "q"):
                    break
                if key == "\n":
                    chosen = options[index][0]
                    break
                if key in ("j", "k"):
                    index = (index + (1 if key == "j" else -1)) % len(options)
                    draw()
        except KeyboardInterrupt:
            pass
        finally:
            sys.stdout.write("\x1b[?25h")
            if chosen is None:
                print("Selection cancelled.")
            sys.stdout.flush()
        return chosen

    # POSIX
    import select
    import termios
    import tty

    fd = sys.stdin.fileno()
    previous = termios.tcgetattr(fd)
    try:
        tty.setcbreak(fd)
        sys.stdout.write("\x1b[?25l")
        draw()
        while True:
            key = os.read(fd, 1)
            if key == b"\x1b":
                sequence = b""
                while len(sequence) < 8 and select.select([fd], [], [], 0.08)[0]:
                    sequence += os.read(fd, 1)
                    if sequence[-1:] in (b"A", b"B", b"C", b"D", b"~"):
                        break
                if not sequence:
                    break
                key = {b"[A": b"k", b"OA": b"k", b"[B": b"j", b"OB": b"j"}.get(
                    sequence, b"ignore"
                )
            if key in (b"", b"q", b"\x03", b"\x04"):
                break
            if key in (b"\n", b"\r"):
                chosen = options[index][0]
                break
            if key in (b"j", b"k"):
                index = (index + (1 if key == b"j" else -1)) % len(options)
                draw()
    except KeyboardInterrupt:
        pass
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, previous)
        sys.stdout.write("\x1b[?25h")
        if chosen is None:
            print("Selection cancelled.")
        sys.stdout.flush()
    return chosen
