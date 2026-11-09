"""Reset the USB device behind a serial port (diagnostics)."""
from __future__ import annotations

USBDEVFS_RESET = 21780  # _IO('U', 20)


def usb_device_dir(port: str) -> str:
    """Return the sysfs directory of the USB device behind a serial port."""
    import os  # pylint: disable=import-outside-toplevel

    tty = os.path.basename(os.path.realpath(port))
    path = os.path.realpath(f"/sys/class/tty/{tty}/device")
    while path not in ("/", "") and not os.path.exists(os.path.join(path, "busnum")):
        path = os.path.dirname(path)
    if not os.path.exists(os.path.join(path, "busnum")):
        raise OSError(f"no USB device found for {port}")
    return path


def usb_reset(port: str, method: str = "ioctl") -> str:
    """Reset the USB device behind a serial port, like unplugging it. Blocking."""
    import fcntl  # pylint: disable=import-outside-toplevel
    import os  # pylint: disable=import-outside-toplevel
    import time  # pylint: disable=import-outside-toplevel

    path = usb_device_dir(port)
    if method == "authorized":
        with open(os.path.join(path, "authorized"), "w", encoding="ascii") as f:
            f.write("0")
        time.sleep(3)
        with open(os.path.join(path, "authorized"), "w", encoding="ascii") as f:
            f.write("1")
        return f"{path} de/re-authorised"
    with open(os.path.join(path, "busnum"), encoding="ascii") as f:
        bus = int(f.read())
    with open(os.path.join(path, "devnum"), encoding="ascii") as f:
        dev = int(f.read())
    node = f"/dev/bus/usb/{bus:03d}/{dev:03d}"
    fd = os.open(node, os.O_WRONLY)
    try:
        fcntl.ioctl(fd, USBDEVFS_RESET, 0)
    finally:
        os.close(fd)
    return f"{node} reset"
