"""Action-free synthetic portfolio replay with separate opening and decision evidence."""

from decimal import Decimal
from typing import Literal

from pydantic import Field, model_validator

from trading_system.backtest.models import Input, Scenario, Time
from trading_system.portfolio.models import (
    CandidateSignal,
    Correlation,
    Instrument,
    Limits,
    StrategyWeight,
)


class SimulationStrategy(StrategyWeight):
    holding_sessions: int = Field(strict=True, ge=1, le=60)


class SimulationFrame(Input):
    decision_at: Time
    opening_instruments: tuple[Instrument, ...]
    opening_correlations: tuple[Correlation, ...]
    decision_instruments: tuple[Instrument, ...]
    decision_correlations: tuple[Correlation, ...]
    signals: tuple[CandidateSignal, ...]


class SimulationInput(Input):
    mode: Literal["synthetic"]
    source_label: str = Field(min_length=1)
    market: Scenario
    strategies: tuple[SimulationStrategy, ...] = Field(min_length=1)
    sessions: tuple[SimulationFrame, ...]
    limits: Limits

    @property
    def initial_cash(self) -> Decimal:
        return self.market.initial_cash

    @model_validator(mode="after")
    def chronology_and_evidence(self):
        if self.market.actions or self.market.signals:
            raise ValueError(
                "Portfolio replay requires action-free market data without market signals"
            )
        if len({strategy.strategy for strategy in self.strategies}) != len(self.strategies):
            raise ValueError("Duplicate simulation strategy")
        if sum((strategy.weight for strategy in self.strategies), Decimal(0)) > 1:
            raise ValueError("Strategy weights cannot exceed equity")
        if len(self.sessions) != len(self.market.sessions):
            raise ValueError("Opening/decision frames must align with every market session")
        declared = set(self.market.declared_codes)
        for frame, session in zip(self.sessions, self.market.sessions, strict=True):
            if frame.decision_at != session.decision_at:
                raise ValueError("Simulation frame decision mismatch")
            for instruments in (frame.opening_instruments, frame.decision_instruments):
                if (
                    len(instruments) != len(declared)
                    or {item.code for item in instruments} != declared
                ):
                    raise ValueError("Every frame must explicitly cover declared instruments")
            opens = {item.code: item for item in frame.opening_instruments}
            closes = {item.code: item for item in frame.decision_instruments}
            rules = {item.code: item for item in session.rules}
            for bar in session.bars:
                opening, closing, rule = opens[bar.code], closes[bar.code], rules[bar.code]
                if bar.open is not None and not (
                    opening.mark_price == bar.open
                    and opening.mark_as_of == session.open_at
                    and opening.mark_known_at == session.open_at
                ):
                    raise ValueError(
                        "Opening mark must match the declared synthetic opening observation"
                    )
                if not (
                    closing.mark_price == bar.close
                    and closing.mark_as_of == bar.close_at
                    and closing.mark_known_at == bar.known_at
                ):
                    raise ValueError("Decision mark must match the causal raw closing observation")
                if not (
                    opening.lot_size == rule.lot_size
                    and opening.short_total_capacity == rule.short_inventory
                    and opening.short_allowed == rule.short_allowed
                    and opening.short_additional_collateral_ratio == rule.margin_ratio
                ):
                    raise ValueError("Opening execution and planning rules disagree")
            if len({(item.strategy, item.code) for item in frame.signals}) != len(frame.signals):
                raise ValueError("Duplicate frame signal")
            for pairs in (frame.opening_correlations, frame.decision_correlations):
                keys = [tuple(sorted((item.code_a, item.code_b))) for item in pairs]
                if len(keys) != len(set(keys)) or any(item.code_a == item.code_b for item in pairs):
                    raise ValueError("Duplicate or self correlation frame pair")
            for signal in frame.signals:
                if signal.decision_at != session.decision_at or signal.code not in session.universe:
                    raise ValueError("Signal must match its dated declared decision universe")
        return self
