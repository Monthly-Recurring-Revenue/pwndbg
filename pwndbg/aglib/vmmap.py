from __future__ import annotations

import bisect
import os
from pathlib import Path

import pwndbg
import pwndbg.aglib
import pwndbg.aglib.macho
import pwndbg.aglib.vmmap_custom
import pwndbg.dbg_mod
import pwndbg.lib.cache
import pwndbg.lib.memory
from pwndbg.dbg_mod import MemoryMap
from pwndbg.lib.arch import Platform
from pwndbg.lib.config import PARAM_BOOLEAN
from pwndbg.lib.config import PARAM_ENUM
from pwndbg.lib.memory import Page

pwndbg.config.add_param(
    "vmmap-prefer-relpaths",
    True,
    "show relative paths by default in vmmap",
    param_class=PARAM_BOOLEAN,
)

vmmap_cache_mode = pwndbg.config.add_param(
    "vmmap-cache-mode",
    "auto",
    "vmmap caching mode: auto (recommended), off, or always",
    help_docstring="""
Controls how vmmap data is cached for performance optimization.

- **auto** (default, recommended): Vmmap is cached until process start or new
  library load (objfile event). This provides a good balance - fast stepping
  while still refreshing when the memory layout actually changes. Use
  `vmmap --refresh` after mmap/munmap syscalls if needed.

- **off**: Vmmap is never automatically fetched. You must run `vmmap` command
  manually to see memory map info. Context display won't show region labels
  or colors. Fastest option but least convenient.

- **always**: Vmmap is refreshed on every stop (original behavior). Always
  shows fresh data but significantly slower on macOS (~150-270ms per stop
  due to LLDB's GetMemoryRegions() being slow).
""",
    param_class=PARAM_ENUM,
    enum_sequence=["auto", "off", "always"],
)


def _refine_memory_map(pages: MemoryMap) -> MemoryMap:
    if not (
        pwndbg.aglib.arch.platform == Platform.DARWIN
        and pwndbg.aglib.macho.shared_cache() is not None
    ):
        return pages

    # Darwin platforms use something called the Shared Cache for system
    # libraries. Debuggers may report mapping ranges that belong to the
    # shared cache in many ways, but we would like to tag those with a
    # little more information.
    final_pages = []

    shared_cache = pwndbg.aglib.macho.shared_cache()
    shared_cache_start = shared_cache.base
    shared_cache_end = shared_cache_start + shared_cache.size

    # images_sorted is already a cached list, no need to convert
    images = shared_cache.images_sorted
    images_base = [image[1] for image in images]

    ptrsize: int = pwndbg.aglib.arch.ptrsize

    for page in pages.ranges():
        if page.end < shared_cache_start or page.start >= shared_cache_end:
            # No overlap with the shared cache.
            final_pages.append(page)
            continue

        # We do not support partial overlaps between other mappings and the
        # shared cache.
        #
        # While conceptually there's nothing stopping these from happening,
        # if we ever encounter such a situation, it likely means that we
        # either got something wrong, or that Darwin/LLDB has changed in
        # such a way that we are likely not able to gracefully handle.
        #
        assert page.start >= shared_cache_start and page.end <= shared_cache_end

        one_past_index = bisect.bisect_right(images_base, page.start)
        curr_base = page.start

        while True:
            if one_past_index > len(images):
                break

            if one_past_index == 0:
                # Indicates that this mapping is not part of any image, but
                # still part of the shared cache itself. Use a special name
                # for it.
                objfile = "[SharedCacheHeader]"
            elif images_base[one_past_index - 1] >= page.end:
                break
            else:
                # Name this mapping after the image it belongs to.
                objfile = images[one_past_index - 1][0].decode("ascii")
                curr_base = max(images_base[one_past_index - 1], page.start)

            if one_past_index == len(images):
                end = page.end
            else:
                end = min(page.end, images_base[one_past_index])

            final_pages.append(
                Page(
                    curr_base,
                    end - curr_base,
                    page.flags,
                    curr_base - shared_cache_start,
                    ptrsize,
                    objfile,
                    in_darwin_shared_cache=True,
                )
            )

            one_past_index += 1

    return type(pages)(final_pages)


# Track if we've shown the "off" mode warning
_vmmap_off_warning_shown = False


@pwndbg.lib.cache.cache_until("start", "objfile")
def _get_memory_map_cached() -> MemoryMap:
    """Internal cached vmmap fetch. Use get_memory_map() instead."""
    return _refine_memory_map(pwndbg.dbg.selected_inferior().vmmap())


def get_memory_map() -> MemoryMap:
    """
    Get the memory map for the current inferior.

    Behavior depends on the `vmmap-cache-mode` config setting:
    - auto (default): Cached until process start or new library load (objfile).
    - off: Returns empty MemoryMap (fastest, use `vmmap` command to fetch manually).
    - always: Always fetches fresh data (slowest, original behavior).

    Use `vmmap --refresh` to force a refresh regardless of mode.

    Returns:
        MemoryMap object containing memory pages
    """
    global _vmmap_off_warning_shown

    mode = str(vmmap_cache_mode)

    if mode == "off":
        # Don't auto-fetch, return empty map
        if not _vmmap_off_warning_shown:
            import pwndbg.color.message as M

            print(
                M.warn(
                    "vmmap-cache-mode is 'off'. Run 'vmmap' to manually fetch memory map, "
                    "or 'set vmmap-cache-mode auto' to enable automatic fetching."
                )
            )
            _vmmap_off_warning_shown = True
        return MemoryMap([])

    if mode == "always":
        # Clear cache to force refresh on every call
        pwndbg.lib.cache.clear_cache(pwndbg.lib.cache.CacheUntilEvent.OBJFILE)

    # Reset warning flag when we actually fetch
    _vmmap_off_warning_shown = False

    return _get_memory_map_cached()


def get() -> tuple[pwndbg.lib.memory.Page, ...]:
    """Get all memory pages. Respects vmmap-cache-mode setting."""
    return tuple(get_memory_map().ranges())


def find(address: int | pwndbg.dbg_mod.Value | None) -> pwndbg.lib.memory.Page | None:
    if address is None:
        return None

    address = int(address)
    if address < 0:
        return None

    page = get_memory_map().lookup_page(address)

    if page is not None:
        return page

    return pwndbg.aglib.vmmap_custom.explore(address)


def addr_region_start(address: int | pwndbg.dbg_mod.Value) -> int | None:
    """
    Let's define a "region" as contiguous memory compromised of memory mappings
    which all have the same object file name. Also referred to as "File (Base)" by
    `xinfo`.

    Returns:
        The start of the memory region this address belongs to, or None if the address
        is not mapped.
    """
    address = int(address)
    if address < 0:
        return None

    mappings = sorted(get(), key=lambda p: p.vaddr)
    idx = -1
    for i in range(len(mappings)):
        if mappings[i].start <= address < mappings[i].end:
            idx = i
            break

    if idx == -1:
        # Maybe we can find the page by exploring.
        explored_page = pwndbg.aglib.vmmap_custom.explore(address)
        if not explored_page:
            return None

        # We know vmmap_custom.explore() can only find one page, it does
        # not cascade a whole region so there is no need to look backwards.
        return explored_page.start

    # Look backwards from i to find all the mappings with the same name.
    objname = mappings[idx].objfile
    while idx > 0 and objname == mappings[idx - 1].objfile:
        idx -= 1

    # There might be other mappings with the name "objname" in the address space
    # but they are not contiguous with us, so we don't care.
    return mappings[idx].start


def named_region_start(mapping_name: str, exact_match: bool = True) -> int | None:
    """
    Returns the lowest address which is mapped with `mapping_name`.

    This works both for object file names and stuff like "[heap]", but note that not
    all mappings with the same name are necessarily contiguous (especially if they
    aren't backed by an object file).

    Will not invoke vmmap_explore.

    If exact_match is True looks for exact path match, otherwise will match
    the os.path.basename()s.
    """
    mappings = sorted(get(), key=lambda p: p.vaddr)

    if exact_match:
        for mapping in mappings:
            # Resolve relative files and symlinks even for exact matches.
            # FIXME: This is a workaround for #3641 . Don't use Path()
            # after that is fixed.
            if Path(mapping.objfile).resolve() == Path(mapping_name).resolve():
                return mapping.start

        return None
    # Note that os.path.basename("[heap]") == "[heap]".
    mapping_basename = os.path.basename(mapping_name)
    for mapping in mappings:
        if os.path.basename(mapping.objfile) == mapping_basename:
            return mapping.start

    return None
