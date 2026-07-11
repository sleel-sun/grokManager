"""ModelSpec — the single source of truth for model metadata."""

from dataclasses import dataclass

from .enums import Capability, ModeId, Tier


@dataclass(slots=True, frozen=True)
class ModelSpec:
    """Immutable descriptor for one model variant.

    ``model_name``  is the public-facing identifier used in API requests.
    ``mode_id``     is the upstream ``modeId`` value (auto / fast / expert).
    ``tier``        determines which account pool is used (basic / super).
                    When ``prefer_best`` is True, ``tier`` only affects
                    ``pool_name()``/``pool_id()``; the actual selection order
                    is reversed (heavy → super → basic).
    ``capability``  is a bitmask of supported operations.
    ``enabled``     gates whether the model appears in ``/v1/models``.
    ``public_name`` is the human-readable display name.
    ``prefer_best`` when True, reverses pool priority to try higher-tier
                    pools first (hard priority, not soft preference).
    ``upstream_profile`` selects the reverse endpoint/protocol family.
    ``upstream_model`` overrides the model string sent to that upstream.
    ``upstream_model_config_key`` optionally lets runtime config override the
                    upstream model string while keeping this value as default.
    ``console_fixed_effort`` forces ``reasoning.effort`` for Console Responses.
    ``console_default_effort`` uses caller effort when provided, otherwise
                    applies this default; explicit ``none`` disables it.
    ``console_input_prefix`` prepends a trigger line for Console modes that are
                    exposed by upstream chat text rather than as model IDs.
    """

    model_name: str
    mode_id: ModeId
    tier: Tier
    capability: Capability
    enabled: bool
    public_name: str
    prefer_best: bool = False
    upstream_profile: str = "grok_web"
    upstream_model: str | None = None
    upstream_model_config_key: str | None = None
    console_fixed_effort: str | None = None
    console_default_effort: str | None = None
    console_input_prefix: str | None = None

    # --- convenience predicates ---

    def is_chat(self) -> bool:
        return bool(self.capability & Capability.CHAT)

    def is_image(self) -> bool:
        return bool(self.capability & Capability.IMAGE)

    def is_image_edit(self) -> bool:
        return bool(self.capability & Capability.IMAGE_EDIT)

    def is_video(self) -> bool:
        return bool(self.capability & Capability.VIDEO)

    def is_voice(self) -> bool:
        return bool(self.capability & Capability.VOICE)

    def upstream_model_name(self) -> str:
        """Return the model identifier to send to the selected upstream."""
        default = self.upstream_model or self.model_name
        key = (self.upstream_model_config_key or "").strip()
        if not key:
            return default

        from app.platform.config.snapshot import get_config

        configured = str(get_config(key, default) or "").strip()
        return configured or default

    def uses_console_responses(self) -> bool:
        """Return whether this model should call console.x.ai Responses."""
        return self.upstream_profile == "console_responses"

    def uses_grok_build_responses(self) -> bool:
        """Return whether this model should call the Grok Build CLI upstream."""
        return self.upstream_profile == "grok_build_responses"

    def uses_responses_protocol(self) -> bool:
        """Return whether this model uses a Responses-compatible upstream."""
        return self.uses_console_responses() or self.uses_grok_build_responses()

    def console_reasoning_effort(self, requested: str | None = None) -> str | None:
        """Resolve the Console Responses reasoning effort for this model."""
        if self.console_fixed_effort:
            return self.console_fixed_effort
        if not self.console_default_effort:
            return None

        normalized = (requested or "").strip().lower()
        if normalized == "none":
            return None
        return normalized or self.console_default_effort

    def pool_name(self) -> str:
        """Return the canonical pool string for this tier."""
        if self.tier == Tier.SUPER:
            return "super"
        if self.tier == Tier.HEAVY:
            return "heavy"
        return "basic"

    def pool_id(self) -> int:
        """Return the integer PoolId for the dataplane account table."""
        return int(self.tier)

    # 返回按优先级排序的候选池 ID
    def pool_candidates(self) -> tuple[int, ...]:
        """Return pool IDs to try in priority order.

        When ``prefer_best`` is True the order is reversed so that the
        highest-tier pool is attempted first (hard priority — the first
        pool with any available account wins; lower pools are only reached
        when all accounts in higher pools are exhausted).

        Default (prefer_best=False):
          BASIC tier  → try basic first, then super, then heavy
          SUPER tier  → try super first, then heavy
          HEAVY tier  → heavy only

        Reversed (prefer_best=True):
          BASIC tier  → try heavy first, then super, then basic
          SUPER tier  → try heavy first, then super
          HEAVY tier  → heavy only
        """
        if self.prefer_best:
            if self.tier == Tier.HEAVY:
                return (2,)  # heavy only
            if self.tier == Tier.SUPER:
                return (2, 1)  # heavy, super
            return (2, 1, 0)  # heavy, super, basic
        if self.tier == Tier.BASIC:
            return (0, 1, 2)  # basic, super, heavy
        if self.tier == Tier.SUPER:
            return (1, 2)  # super, heavy
        return (2,)  # heavy only


__all__ = ["ModelSpec"]
