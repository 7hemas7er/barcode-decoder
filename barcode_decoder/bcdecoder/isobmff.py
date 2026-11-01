"""The pixel size of a HEIF/HEIC file, read from its header before anything is decoded.

A HEIF file is an ISO base media file (ISO/IEC 14496-12): a list of boxes. The size of each image is its `ispe`
property (ISO/IEC 23008-12 6.5.3), found through `meta` > `iprp` > `ipco` (the properties, numbered from 1) and
`ipma` (which properties belong to which item); `pitm` names the primary item. An iPhone photo is a `grid` item of
512x512 tiles: the grid's `ispe` is the size of the photo.

The reader trusts nothing in the file: no recursion (the boxes it needs sit at fixed depths), every size is checked
against its parent box and against the buffer, and the number of boxes and associations it looks at is capped. On
anything it does not understand it returns None, and the caller falls back to libheif's own header parse.
"""

HEIF_BRANDS = frozenset((b"heic", b"heix", b"hevc", b"hevx", b"heim", b"heis", b"mif1", b"msf1"))
MAX_BOXES = 4096            # boxes visited in one file
MAX_ASSOCIATIONS = 65536    # ipma entries + associations visited in one file
MAX_SIDE = 1 << 31          # an ispe side above this is nonsense: give up


def is_heif(head):
    """True for the first bytes of a HEIF/HEIC file: an `ftyp` box whose major brand is a HEIF image brand.
    AVIF (`avif`), MP4 (`isom`, `mp42`) and the other ISO brands are not."""
    return len(head) >= 12 and head[4:8] == b"ftyp" and bytes(head[8:12]) in HEIF_BRANDS


class _Budget:
    def __init__(self):
        self.boxes = MAX_BOXES
        self.assoc = MAX_ASSOCIATIONS

    def box(self):
        self.boxes -= 1
        if self.boxes < 0:
            raise _GiveUp("too many boxes")

    def association(self, n=1):
        self.assoc -= n
        if self.assoc < 0:
            raise _GiveUp("too many associations")


class _GiveUp(Exception):
    pass


def _u(buf, pos, n, end):
    if pos < 0 or pos + n > end or pos + n > len(buf):
        raise _GiveUp("read past the box")
    return int.from_bytes(buf[pos:pos + n], "big")


def _children(buf, start, end, budget):
    """(type, payload start, box end) of the boxes in buf[start:end], in order. A malformed header ends the list."""
    out = []
    pos = start
    end = min(end, len(buf))
    while pos + 8 <= end:
        budget.box()
        size = _u(buf, pos, 4, end)
        kind = bytes(buf[pos + 4:pos + 8])
        header = 8
        if size == 1:                              # 64-bit largesize
            size = _u(buf, pos + 8, 8, end)
            header = 16
        elif size == 0:                            # to the end of the parent
            size = end - pos
        if size < header or pos + size > end:
            break
        if kind == b"uuid":
            header += 16
            if header > size:
                break
        out.append((kind, pos + header, pos + size))
        pos += size
    return out


def _first(boxes, kind):
    for k, start, end in boxes:
        if k == kind:
            return start, end
    return None


def heif_sizes(buf):
    """(primary, largest) from the header of a HEIF file held in `buf` (bytes or memoryview):
    - primary: (width, height) of the `ispe` of the primary item, or None if it cannot be found;
    - largest: the (width, height) with the most pixels among all the `ispe` properties, or None if there is none.
    Never raises on bad input."""
    try:
        return _heif_sizes(memoryview(buf), _Budget())
    except (_GiveUp, ValueError, IndexError, TypeError):
        return None, None


def _heif_sizes(buf, budget):
    top = _children(buf, 0, len(buf), budget)
    if not top or top[0][0] != b"ftyp":
        return None, None
    meta = _first(top, b"meta")
    if meta is None:
        return None, None
    m_start, m_end = meta
    meta_boxes = _children(buf, m_start + 4, m_end, budget)      # meta is a FullBox: version + flags first

    primary_id = None
    pitm = _first(meta_boxes, b"pitm")
    if pitm is not None:
        version = _u(buf, pitm[0], 1, pitm[1])
        primary_id = _u(buf, pitm[0] + 4, 2 if version == 0 else 4, pitm[1])

    iprp = _first(meta_boxes, b"iprp")
    if iprp is None:
        return None, None
    iprp_boxes = _children(buf, iprp[0], iprp[1], budget)
    ipco = _first(iprp_boxes, b"ipco")
    if ipco is None:
        return None, None
    properties = _children(buf, ipco[0], ipco[1], budget)        # index 1 is properties[0]

    sizes = {}                                                   # property index -> (w, h), ispe only
    for index, (kind, start, end) in enumerate(properties, 1):
        if kind == b"ispe":
            w = _u(buf, start + 4, 4, end)
            h = _u(buf, start + 8, 4, end)
            if w > MAX_SIDE or h > MAX_SIDE:
                raise _GiveUp("ispe side")
            sizes[index] = (w, h)
    largest = max(sizes.values(), key=lambda s: s[0] * s[1], default=None)

    primary = None
    if primary_id is not None:
        for kind, start, end in iprp_boxes:
            if kind != b"ipma" or primary is not None:
                continue
            version = _u(buf, start, 1, end)
            flags = _u(buf, start + 1, 3, end)
            count = _u(buf, start + 4, 4, end)
            pos = start + 8
            id_len = 2 if version < 1 else 4
            assoc_len = 2 if flags & 1 else 1
            for _ in range(count):
                budget.association()
                item = _u(buf, pos, id_len, end)
                n = _u(buf, pos + id_len, 1, end)
                pos += id_len + 1
                budget.association(n)
                if item == primary_id:
                    for j in range(n):
                        a = _u(buf, pos + j * assoc_len, assoc_len, end)
                        index = a & (0x7FFF if assoc_len == 2 else 0x7F)
                        if index in sizes:
                            primary = sizes[index]
                            break
                    break
                pos += n * assoc_len
    return primary, largest
