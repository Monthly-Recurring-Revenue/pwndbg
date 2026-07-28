from __future__ import annotations

import argparse
import contextlib
import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from socket import SO_REUSEADDR
from socket import SOL_SOCKET
from socket import socket

from .images import KIMAGES_DIR
from .images import KernelImage
from .images import discover_images


@dataclass(frozen=True)
class KernelConfig:
    image: KernelImage
    variant: str | None = None
    extra_qemu_args: tuple[str, ...] = ()

    @property
    def id(self) -> str:
        """Unique name of the config, e.g. "linux-6.6.18-x86_64+la57"."""
        if self.variant is None:
            return self.image.name
        return f"{self.image.name}+{self.variant}"


def test_configs(images: list[KernelImage]) -> list[KernelConfig]:
    configs = []
    for image in images:
        configs.append(KernelConfig(image))

        if image.arch == "x86_64":
            # additional test with extra QEMU flags (la57: 5-level page tables)
            configs.append(KernelConfig(image, "la57", ("-cpu", "qemu64,+la57")))

    return configs


def qemu_command(
    config: KernelConfig,
    gdb_port: int,
    cmdline_extra: str = "",
    writable: bool = False,
) -> list[str]:
    image = config.image

    if image.arch == "arm64":
        binary = "qemu-system-aarch64"
        # The virt board's default CPU is the 32-bit cortex-a15, so an AArch64
        # guest needs an explicit CPU type; "max" is the best possible emulation with TCG
        # https://www.qemu.org/docs/master/system/arm/virt.html#:~:text=same%20as%20host
        machine_args = ["-machine", "virt", "-cpu", "max"]
        # The virt board's UART is a PL011, which Linux names ttyAMA0.
        # https://www.qemu.org/docs/master/system/arm/virt.html#:~:text=PL011%20UART
        # https://elixir.bootlin.com/linux/v6.6/source/drivers/tty/serial/amba-pl011.c#:~:text=%22ttyAMA%22
        console = "console=ttyAMA0"
    elif image.arch == "x86_64":
        binary = "qemu-system-x86_64"
        machine_args = []
        # 8250.nr_uarts=1 limits the 8250 driver to registering a single
        # serial port (the console on ttyS0).
        # https://elixir.bootlin.com/linux/v6.6/source/drivers/tty/serial/8250/8250_core.c#:~:text=Maximum%20number%20of%20UARTs
        console = "8250.nr_uarts=1 console=ttyS0"
    else:
        raise ValueError(f"no QEMU configuration known for architecture {image.arch!r}")

    # nokaslr: keep the kernel at its link-time addresses, so that symbols
    # from the vmlinux file match the running guest.
    cmdline = f"{console} root=/dev/vda nokaslr"
    if cmdline_extra:
        cmdline += f" {cmdline_extra}"

    command = [
        binary,
        *machine_args,
        "-kernel",
        str(image.boot_image),
        "-nographic",
        "-drive",
        f"file={image.rootfs},if=virtio,format=qcow2",
        # Freeze the CPU at startup; the test runner un-freezes the guest
        # through the gdbstub once a debugger is attached.
        "-S",
        "-gdb",
        # the gdbstub is unauthenticated, so keep it loopback-only
        # https://www.qemu.org/docs/master/system/gdb.html#:~:text=not%20protected
        f"tcp:127.0.0.1:{gdb_port}",
    ]

    if not writable:
        # don't let guest writes modify the shared rootfs image
        command.append("-snapshot")

    command += [*config.extra_qemu_args, "-append", cmdline]

    return command


def reserve_port(ip: str = "127.0.0.1") -> int:
    """Bind to an ephemeral port, force it into the TIME_WAIT state, and unbind
    it, so that only an explicit SO_REUSEADDR bind (like QEMU's gdbstub) can
    take it. Copied from the same helper in tests/library/qemu_user/conftest.py
    """
    with contextlib.closing(socket()) as s:
        s.setsockopt(SOL_SOCKET, SO_REUSEADDR, 1)
        s.bind((ip, 0))

        # the connect below deadlocks on kernel >= 4.4.0 unless this arg is greater than zero
        s.listen(1)

        sockname = s.getsockname()

        # these three are necessary just to get the port into a TIME_WAIT state
        with contextlib.closing(socket()) as s2:
            s2.connect(sockname)
            sock, _ = s.accept()
            with contextlib.closing(sock):
                return int(sockname[1])


class QemuVM:
    def __init__(self, config: KernelConfig):
        self.config = config
        self.gdb_port = reserve_port()

        command = qemu_command(config, self.gdb_port)
        if shutil.which(command[0]) is None:
            raise RuntimeError(
                f"'{command[0]}' not found in PATH. Install the qemu-system "
                "packages for x86 and ARM (./setup-dev.sh does)."
            )

        # keep the guest's serial console for debugging boot hangs and failed tests
        fd, log_path = tempfile.mkstemp(prefix=f"pwndbg-qemu-{config.id}-", suffix=".log")
        self.console_log = Path(log_path)

        try:
            self.process = subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=fd,
                stderr=subprocess.STDOUT,
            )
        finally:
            os.close(fd)

    def alive(self) -> bool:
        return self.process.poll() is None

    def kill(self) -> None:
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()


def main() -> None:
    argv = sys.argv[1:]
    qemu_extra_args: list[str] = []
    if "--" in argv:
        split_at = argv.index("--")
        qemu_extra_args = argv[split_at + 1 :]
        argv = argv[:split_at]

    parser = argparse.ArgumentParser(
        description="Boot one of the downloaded test kernels under QEMU, frozen "
        "at startup, and wait for a GDB connection.",
        epilog="Options after '--' will be passed to QEMU.",
    )
    parser.add_argument(
        "--kernel",
        required=True,
        help="name of the kernel to boot, e.g. linux-6.6.18-x86_64",
    )
    parser.add_argument(
        "--append",
        default="",
        help="extra kernel command line arguments",
    )
    parser.add_argument(
        "--gdb-port",
        type=int,
        default=1234,
        help="TCP port for the gdbstub (default: %(default)s)",
    )
    parser.add_argument(
        "--writable", action="store_true", help="let the guest write to the root filesystem image"
    )
    args = parser.parse_args(argv)

    images = discover_images(KIMAGES_DIR)
    if not images:
        print(
            f"No kernel images found in {KIMAGES_DIR}.\n"
            "Download them first with: ./tests/library/qemu_system/download-kernel-images.sh",
            file=sys.stderr,
        )
        sys.exit(1)

    image = next((image for image in images if image.name == args.kernel), None)
    if image is None:
        available = "\n".join(f"    {image.name}" for image in images)
        print(f"Unknown kernel {args.kernel!r}. Available kernels:\n{available}", file=sys.stderr)
        sys.exit(1)

    config = KernelConfig(image, extra_qemu_args=tuple(qemu_extra_args))
    command = qemu_command(config, args.gdb_port, args.append, args.writable)

    print("Waiting for GDB to attach (use 'ctrl-a x' to quit):")
    print(f"    gdb {image.vmlinux} -ex 'target remote :{args.gdb_port}'")
    sys.stdout.flush()
    os.execvp(command[0], command)


if __name__ == "__main__":
    main()
