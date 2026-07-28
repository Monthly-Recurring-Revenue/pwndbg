from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

KIMAGES_DIR = Path(__file__).resolve().parent / "kimages"


@dataclass(frozen=True)
class KernelImage:
    name: str
    type: str
    version: str
    arch: str
    vmlinux: Path
    boot_image: Path
    rootfs: Path


def _parse_kernel_name(name: str) -> tuple[str, str, str]:
    # name = <KERNEL_TYPE>-<KERNEL_VERSION>-<ARCH>
    # e.g. "linux-5.10.178-arm64" or "ack-android13-5.10-lts-x86_64"
    # extract architecture as last dash-separated group of the kernel's name
    arch = name.rsplit("-", 1)[-1]
    version_match = re.search(r"\d+\.\d+(?:\.\d+)?(?:-lts)?", name)
    if version_match is None:
        raise ValueError(f"no kernel version found in kernel name {name!r}")
    version = version_match.group()

    suffix = f"-{version}-{arch}"
    ktype = name.removesuffix(suffix)
    if not ktype or ktype == name:
        raise ValueError(f"kernel name {name!r} does not end in {suffix!r}")

    if arch == "aarch64":
        arch = "arm64"

    return ktype, version, arch


def discover_images(images_dir: Path) -> list[KernelImage]:
    images = []
    for vmlinux in sorted(images_dir.glob("vmlinux-*")):
        name = vmlinux.name.removeprefix("vmlinux-")

        try:
            ktype, version, arch = _parse_kernel_name(name)
        except ValueError as e:
            print(f"WARNING: ignoring {vmlinux}: {e}")
            continue

        boot_images = sorted(images_dir.glob(f"*Image-{name}"))
        if not boot_images:
            print(f"WARNING: ignoring {vmlinux}: no matching boot image (bzImage/Image)")
            continue

        rootfses = sorted(images_dir.glob(f"*-{arch}.img"))
        if not rootfses:
            print(f"WARNING: ignoring {vmlinux}: no root filesystem image (*-{arch}.img)")
            continue

        images.append(
            KernelImage(
                name=name,
                type=ktype,
                version=version,
                arch=arch,
                vmlinux=vmlinux,
                boot_image=boot_images[0],
                rootfs=rootfses[0],
            )
        )

    return images
