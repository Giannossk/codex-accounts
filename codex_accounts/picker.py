"""Terminal picker with arrow keys and a numbered-input fallback."""

import os
import select
import shutil
import sys


def pick(options: list[tuple[str, str]], *, selected: str | None = None) -> str | None:
    if not options:
        return None
    if (
        not (sys.stdin.isatty() and sys.stdout.isatty())
        or os.environ.get("TERM") == "dumb"
        or os.name != "posix"
    ):
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

    import termios
    import tty

    fd = sys.stdin.fileno()
    previous = termios.tcgetattr(fd)
    index = next((i for i, (key, _) in enumerate(options) if key == selected), 0)
    drawn = 0
    chosen = None

    def draw() -> None:
        nonlocal drawn
        size = shutil.get_terminal_size()
        visible = min(len(options), max(1, size.lines - 6))
        start = min(max(0, index - visible // 2), len(options) - visible)
        if drawn:
            sys.stdout.write(f"\x1b[{drawn}A\r")
        lines = ["Select an account", ""]
        for i in range(start, start + visible):
            mark = ">" if i == index else " "
            lines.append(f" {mark} {i + 1}. {options[i][1]}")
        lines += ["", "Up/Down or j/k | Enter select | Esc cancel"]
        for line in lines:
            sys.stdout.write("\x1b[2K" + line[: max(1, size.columns - 1)] + "\r\n")
        for _ in range(max(0, drawn - len(lines))):
            sys.stdout.write("\x1b[2K\r\n")
        if drawn > len(lines):
            sys.stdout.write(f"\x1b[{drawn - len(lines)}A")
        drawn = len(lines)
        sys.stdout.flush()

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
