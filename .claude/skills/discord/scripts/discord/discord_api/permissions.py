"""Whether you can see a server channel and read its history, worked out the way Discord does from roles and overwrites."""

from __future__ import annotations

ADMINISTRATOR = 1 << 3
VIEW_CHANNEL = 1 << 10
READ_MESSAGE_HISTORY = 1 << 16


def channel_access(*, server_id: int, owner_id: int, my_id: int, my_roles: list[int], role_permissions: dict[int, int],
                   overwrites: list[dict]) -> tuple[bool, bool]:
    """(can view, can read history) for one channel.

    `role_permissions` maps every role id, @everyone (whose id is the server's) included, to its permission bits; `overwrites` are the channel's raw permission_overwrites (type 0 role, 1 member). Order: owner or administrator sees everything; else the base is @everyone's bits OR your roles' bits, then @everyone's overwrite, then your roles' overwrites (all denies, then all allows), then your own overwrite.
    """
    if my_id == owner_id:
        return True, True
    base = role_permissions.get(server_id, 0)
    for r in my_roles:
        base |= role_permissions.get(r, 0)
    if base & ADMINISTRATOR:
        return True, True
    by_target = {(int(o.get("type", 0)), int(o["id"])): o for o in overwrites}

    def apply(bits: int, o: dict | None) -> int:
        if not o:
            return bits
        return (bits & ~int(o.get("deny", 0))) | int(o.get("allow", 0))

    bits = apply(base, by_target.get((0, server_id)))
    deny = allow = 0
    for r in my_roles:
        o = by_target.get((0, r))
        if o:
            deny |= int(o.get("deny", 0))
            allow |= int(o.get("allow", 0))
    bits = (bits & ~deny) | allow
    bits = apply(bits, by_target.get((1, my_id)))
    view = bool(bits & VIEW_CHANNEL)
    return view, view and bool(bits & READ_MESSAGE_HISTORY)
