"""Scannable sources: servers, friends, friend requests and group DMs.

A guild's members have to be prised out of the gateway a sidebar page at a
time.  Friends and group DMs need one REST call and come back complete -- there
is no visibility question and no coverage caveat, so those scans are both fast
and exhaustive.

Everything downstream (the scan pipeline, the results table, exports) works off
:class:`ScanSource` rather than a guild, so the three behave identically once a
member list exists.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .discord import (
    FRIEND,
    GROUP_DM_CHANNEL,
    INCOMING_REQUEST,
    Guild,
    GuildMember,
    PrivateChannel,
    Relationship,
)

KIND_GUILD = "guild"
KIND_FRIENDS = "friends"
KIND_REQUESTS = "requests"
KIND_GROUP = "group"
KIND_INBOX = "inbox"

#: display order in the source list
KIND_ORDER = {
    KIND_INBOX: 0,
    KIND_REQUESTS: 1,
    KIND_FRIENDS: 2,
    KIND_GROUP: 3,
    KIND_GUILD: 4,
}

KIND_LABEL = {
    KIND_GUILD: "server",
    KIND_FRIENDS: "friends",
    KIND_REQUESTS: "requests",
    KIND_GROUP: "group DM",
    KIND_INBOX: "live",
}

#: how the sources pane groups the list, in display order
GROUPS: list[tuple[str, tuple[str, ...]]] = [
    ("Live", (KIND_INBOX,)),
    ("People", (KIND_REQUESTS, KIND_FRIENDS)),
    ("Group DMs", (KIND_GROUP,)),
    ("Servers", (KIND_GUILD,)),
]


def group_for(kind: str) -> str:
    for title, kinds in GROUPS:
        if kind in kinds:
            return title
    return "Other"


@dataclass
class ScanSource:
    kind: str
    id: str
    name: str
    member_count: int | None = None
    guild: Guild | None = None
    channel: PrivateChannel | None = None
    #: pre-resolved members, for sources that do not need the gateway
    members: list[GuildMember] = field(default_factory=list)

    @property
    def label(self) -> str:
        return KIND_LABEL.get(self.kind, self.kind)

    @property
    def needs_gateway(self) -> bool:
        """Guilds need the member sidebar; the inbox needs a live feed."""
        return self.kind in (KIND_GUILD, KIND_INBOX)

    @property
    def is_live(self) -> bool:
        """Whether this source never finishes on its own."""
        return self.kind == KIND_INBOX

    @property
    def is_complete(self) -> bool:
        """Whether a scan of this source can see every member."""
        return not self.needs_gateway

    @property
    def sort_key(self) -> tuple:
        return (
            KIND_ORDER.get(self.kind, 9),
            -(self.member_count or 0),
            self.name.lower(),
        )


def _member_from_user(raw: dict, nickname: str | None = None) -> GuildMember | None:
    if not raw.get("id"):
        return None
    return GuildMember(
        id=str(raw["id"]),
        username=raw.get("username") or "unknown",
        global_name=raw.get("global_name"),
        discriminator=str(raw.get("discriminator") or "0"),
        nick=nickname,
        bot=bool(raw.get("bot")),
        avatar=raw.get("avatar"),
    )


def _member_from_relationship(entry: Relationship) -> GuildMember:
    return GuildMember(
        id=entry.user_id,
        username=entry.username,
        global_name=entry.global_name,
        discriminator=entry.discriminator,
        nick=entry.nickname,
        bot=entry.bot,
        avatar=entry.avatar,
    )


def build_sources(
    guilds: list[Guild],
    relationships: list[Relationship] | None = None,
    private_channels: list[PrivateChannel] | None = None,
    is_bot: bool = False,
) -> list[ScanSource]:
    """Assemble every scannable source, newest-relevance first.

    Friend requests come first: someone who has just added you is precisely who
    you want checked before deciding, and the list is usually short.

    ``is_bot`` drops everything a bot application has no access to. Servers are
    all it can see; the live inbox additionally needs the message-content
    intent, which is far more than a member scanner has any business asking
    for. Listing sources that could only ever fail would be worse than not
    offering them.
    """
    sources: list[ScanSource] = []
    if not is_bot:
        sources.append(
            ScanSource(
                kind=KIND_INBOX,
                id="inbox",
                name="Incoming messages",
                member_count=None,
            )
        )

    friends = [r for r in (relationships or []) if r.type == FRIEND]
    if friends:
        sources.append(
            ScanSource(
                kind=KIND_FRIENDS,
                id="friends",
                name="Friends",
                member_count=len(friends),
                members=[_member_from_relationship(r) for r in friends],
            )
        )

    incoming = [r for r in (relationships or []) if r.type == INCOMING_REQUEST]
    if incoming:
        sources.append(
            ScanSource(
                kind=KIND_REQUESTS,
                id="requests",
                name="Incoming friend requests",
                member_count=len(incoming),
                members=[_member_from_relationship(r) for r in incoming],
            )
        )

    for channel in private_channels or []:
        if channel.type != GROUP_DM_CHANNEL:
            continue
        members = [
            member
            for member in (_member_from_user(r) for r in channel.recipients)
            if member is not None
        ]
        sources.append(
            ScanSource(
                kind=KIND_GROUP,
                id=channel.id,
                name=channel.display_name(),
                member_count=len(members),
                channel=channel,
                members=members,
            )
        )

    for guild in guilds:
        sources.append(
            ScanSource(
                kind=KIND_GUILD,
                id=guild.id,
                name=guild.name,
                member_count=guild.member_count,
                guild=guild,
            )
        )

    sources.sort(key=lambda s: s.sort_key)
    return sources
