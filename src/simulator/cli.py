"""``homestead-simulator`` -- run a simulated homestead against a broker or offline.

Examples::

    homestead-simulator --list-scenarios
    homestead-simulator --scenario clear_summer_day --duration 24h --speed 10000 --offline
    homestead-simulator --scenario generator_support --broker 127.0.0.1 --port 1883 --speed 60

``--offline`` uses the in-process bus, so the whole rig runs with no broker at
all -- SDD section 19 step 1 bench testing on a laptop. Everything else is
identical, including the topics and payloads, so a scenario validated offline
behaves the same against Mosquitto.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from collections import Counter
from typing import Any, Sequence

from homestead_twin.config import Settings
from homestead_twin.mqtt import InMemoryBus, Message, PahoBus
from simulator.clock import format_duration, parse_duration
from simulator.scenarios import ScenarioRunner, get_scenario, scenario_names
from simulator.site import SimulatedSite

logger = logging.getLogger("simulator")

#: Published messages are folded into counters and then dropped, so a 24-hour
#: run does not accumulate half a million envelopes in memory.
_FLUSH_EVERY_STEPS = 20


class TopicCollector:
    """Counts what went past on the bus without retaining the payloads."""

    def __init__(self) -> None:
        self.topics: Counter[str] = Counter()
        self.kinds: Counter[str] = Counter()

    def __call__(self, message: Message) -> None:
        self.topics[message.topic] += 1
        parts = message.topic.split("/")
        if "availability" in parts:
            self.kinds["availability"] += 1
        elif "ack" in parts:
            self.kinds["command_ack"] += 1
        elif "cmd" in parts:
            self.kinds["command"] += 1
        elif "event" in parts:
            self.kinds["event"] += 1
        else:
            self.kinds["telemetry"] += 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="homestead-simulator",
        description="Simulated homestead MQTT site for the Homestead Digital Twin.",
    )
    parser.add_argument("--scenario", default="clear_summer_day", help="scenario name to run")
    parser.add_argument(
        "--list-scenarios", action="store_true", help="list the available scenarios and exit"
    )
    parser.add_argument(
        "--duration",
        default=None,
        help="simulated duration, e.g. 24h, 90m, 3600 (default: the scenario's own)",
    )
    parser.add_argument(
        "--step", type=float, default=None, help="simulated seconds per step (default: scenario)"
    )
    parser.add_argument(
        "--speed",
        type=float,
        default=None,
        help="simulated seconds per wall-clock second; omit or 0 to run flat out",
    )
    parser.add_argument("--seed", type=int, default=None, help="random seed (default: scenario's)")
    parser.add_argument("--broker", default=None, help="MQTT broker host")
    parser.add_argument("--port", type=int, default=None, help="MQTT broker port")
    parser.add_argument("--base-topic", default=None, help="MQTT base topic (default: homestead)")
    parser.add_argument(
        "--offline",
        action="store_true",
        help="use the in-process bus instead of a broker and print a summary",
    )
    parser.add_argument("--json", action="store_true", help="emit the summary as JSON")
    parser.add_argument("--quiet", action="store_true", help="suppress progress logging")
    parser.add_argument(
        "--verbose", action="store_true", help="log a state line every simulated 30 minutes"
    )
    return parser


def _list_scenarios() -> str:
    lines = ["Available scenarios:"]
    for name in scenario_names():
        scenario = get_scenario(name)
        verifies = ", ".join(scenario.verifies) or "-"
        lines.append(
            f"  {name:<26} {format_duration(scenario.duration_s):>5}  "
            f"step {scenario.step_s:g}s  verifies {verifies}"
        )
        lines.append(f"      {scenario.description}")
    return "\n".join(lines)


def build_site(
    args: argparse.Namespace, scenario
) -> tuple[SimulatedSite, Any, TopicCollector | None]:
    """Construct the bus and the site for ``scenario`` (plus an offline collector)."""
    settings_kwargs: dict[str, Any] = {}
    if args.broker:
        settings_kwargs["mqtt_host"] = args.broker
    if args.port:
        settings_kwargs["mqtt_port"] = args.port
    if args.base_topic:
        settings_kwargs["mqtt_base_topic"] = args.base_topic
    settings_kwargs["mqtt_enabled"] = not args.offline
    settings_kwargs["mqtt_client_id"] = "homestead-simulator"
    settings = Settings(**settings_kwargs)

    collector: TopicCollector | None = None
    if args.offline:
        bus = InMemoryBus()
        collector = TopicCollector()
        bus.subscribe("#", collector)
    else:
        bus = PahoBus(settings)
    bus.start()

    if args.base_topic:
        scenario.config.base_topic = args.base_topic
    site = scenario.build(bus, settings=settings, seed=args.seed)
    return site, bus, collector


def run(args: argparse.Namespace) -> dict[str, Any]:
    """Run one scenario and return the summary dictionary."""
    scenario = get_scenario(args.scenario)
    site, bus, collector = build_site(args, scenario)
    runner = ScenarioRunner(site, scenario)

    duration_s = parse_duration(args.duration) if args.duration else scenario.duration_s
    step_s = args.step if args.step else scenario.step_s
    speed = args.speed if args.speed and args.speed > 0 else None

    progress_interval_s = 1800.0
    state: dict[str, float] = {"next_log": progress_interval_s}

    def on_step(current: SimulatedSite, _balance) -> None:
        runner.pump()
        if isinstance(bus, InMemoryBus) and current.clock.steps % _FLUSH_EVERY_STEPS == 0:
            bus.clear()
        if args.verbose and current.clock.elapsed_s >= state["next_log"]:
            state["next_log"] += progress_interval_s
            snapshot = current.snapshot()
            logger.info(
                "%s  soc=%.1f%%  pv=%.1fkW  load=%.1fkW  gen=%s  container=%.1fC",
                snapshot["time"],
                snapshot["soc_pct"],
                snapshot["pv_ac_kw"],
                snapshot["load_kw"],
                snapshot["generator_state"],
                snapshot["container_temperature_c"],
            )

    site.start()
    runner.pump()
    try:
        site.run(duration_s=duration_s, dt_s=step_s, speed=speed, on_step=on_step)
    finally:
        site.stop()
        bus.stop()

    summary = build_summary(site, scenario, runner, collector, duration_s, step_s)
    return summary


def build_summary(
    site: SimulatedSite,
    scenario,
    runner: ScenarioRunner,
    collector: TopicCollector | None,
    duration_s: float,
    step_s: float,
) -> dict[str, Any]:
    stats = site.stats
    summary: dict[str, Any] = {
        "scenario": scenario.name,
        "description": scenario.description,
        "verifies": list(scenario.verifies),
        "seed": site.config.seed,
        "simulated_duration": format_duration(duration_s),
        "step_s": step_s,
        "steps": stats.steps,
        "assets": len(site.asset_ids()),
        "declared_points": len(site.point_ids()),
        "messages": {
            "telemetry": stats.telemetry_messages,
            "availability": stats.availability_messages,
            "events": stats.event_messages,
            "command_acks": stats.acks_published,
        },
        "commands": {
            "received": stats.commands_received,
            "accepted": stats.commands_accepted,
            "rejected": stats.commands_rejected,
        },
        "energy_kwh": {
            "pv_delivered": round(stats.pv_energy_kwh, 2),
            "load_served": round(stats.load_energy_kwh, 2),
            "curtailed": round(stats.curtailed_energy_kwh, 2),
            "unserved": round(stats.unserved_energy_kwh, 2),
            "generator": round(stats.generator_energy_kwh, 2),
            "battery_charged": round(site.battery.energy_charged_kwh, 2),
            "battery_discharged": round(site.battery.energy_discharged_kwh, 2),
        },
        "battery": {
            "soc_start_pct": round(site.config.battery.initial_soc_pct, 2),
            "soc_end_pct": round(site.battery.soc_pct, 2),
            "soc_min_pct": round(stats.soc_min_pct, 2),
            "soc_max_pct": round(stats.soc_max_pct, 2),
            "cell_temperature_c": round(site.battery.cell_temperature_c, 2),
            "conservation_error_kwh": round(site.battery.conservation_error_kwh(), 9),
        },
        "peaks_kw": {
            "pv": round(stats.pv_peak_kw, 2),
            "load": round(stats.load_peak_kw, 2),
        },
        "generator": {
            "state": site.generator.state,
            "starts": site.generator.starts,
            "failed_attempts": site.generator.failed_attempts,
            "runtime_h": round(site.generator.runtime_h, 3),
            "fuel_pct": round(site.generator.fuel_pct, 2),
        },
        "container_temperature_c": round(site.rack.container_temperature_c, 2),
        "blackout": format_duration(stats.blackout_s),
        "black_start_stage": site.context.black_start_stage,
        "scenario_events": [f"{format_duration(t)}: {label}" for t, label in runner.fired],
        "rejections": stats.rejections[:10],
    }
    if collector is not None:
        summary["distinct_topics"] = len(collector.topics)
        summary["message_kinds"] = dict(collector.kinds)
    return summary


def format_summary(summary: dict[str, Any]) -> str:
    lines = [
        "",
        "=" * 72,
        f"  SIMULATED HOMESTEAD -- {summary['scenario']}",
        "=" * 72,
        f"  {summary['description']}",
        "",
        f"  seed {summary['seed']}   simulated {summary['simulated_duration']}"
        f" in {summary['steps']} steps of {summary['step_s']:g}s",
        f"  verifies: {', '.join(summary['verifies']) or '-'}",
        "",
        f"  assets simulated       {summary['assets']}",
        f"  distinct points        {summary['declared_points']}",
    ]
    if "distinct_topics" in summary:
        lines.append(f"  distinct topics seen   {summary['distinct_topics']}")
    messages = summary["messages"]
    lines += [
        f"  telemetry messages     {messages['telemetry']}",
        f"  availability messages  {messages['availability']}",
        f"  events                 {messages['events']}",
        f"  command acks           {messages['command_acks']}",
        "",
        "  Energy (kWh)",
        f"    PV delivered   {summary['energy_kwh']['pv_delivered']:>9.2f}"
        f"    curtailed  {summary['energy_kwh']['curtailed']:>9.2f}",
        f"    load served    {summary['energy_kwh']['load_served']:>9.2f}"
        f"    unserved   {summary['energy_kwh']['unserved']:>9.2f}",
        f"    generator      {summary['energy_kwh']['generator']:>9.2f}"
        f"    charged    {summary['energy_kwh']['battery_charged']:>9.2f}"
        f"   discharged {summary['energy_kwh']['battery_discharged']:>9.2f}",
        "",
        "  Battery",
        f"    SOC {summary['battery']['soc_start_pct']:.1f}% -> "
        f"{summary['battery']['soc_end_pct']:.1f}% "
        f"(min {summary['battery']['soc_min_pct']:.1f}%, "
        f"max {summary['battery']['soc_max_pct']:.1f}%)",
        f"    cell temperature {summary['battery']['cell_temperature_c']:.1f} C"
        f"    energy-balance residual "
        f"{summary['battery']['conservation_error_kwh']:.2e} kWh",
        "",
        "  Peaks",
        f"    PV {summary['peaks_kw']['pv']:.1f} kW      load {summary['peaks_kw']['load']:.1f} kW",
        "",
        "  Generator",
        f"    state {summary['generator']['state']}   starts "
        f"{summary['generator']['starts']}   failed attempts "
        f"{summary['generator']['failed_attempts']}   runtime "
        f"{summary['generator']['runtime_h']:.2f} h   fuel "
        f"{summary['generator']['fuel_pct']:.0f}%",
        "",
        f"  container {summary['container_temperature_c']:.1f} C"
        f"   blackout {summary['blackout']}"
        f"   black-start stage {summary['black_start_stage']}",
        f"  commands received {summary['commands']['received']}"
        f"  accepted {summary['commands']['accepted']}"
        f"  rejected {summary['commands']['rejected']}",
    ]
    if summary["scenario_events"]:
        lines.append("")
        lines.append("  Injected events")
        for entry in summary["scenario_events"]:
            lines.append(f"    {entry}")
    if summary["rejections"]:
        lines.append("")
        lines.append("  Rejected commands")
        for entry in summary["rejections"]:
            lines.append(f"    {entry}")
    lines.append("=" * 72)
    lines.append("")
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.WARNING if args.quiet else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )

    if args.list_scenarios:
        print(_list_scenarios())
        return 0

    try:
        get_scenario(args.scenario)
    except KeyError as exc:
        parser.error(str(exc))

    summary = run(args)
    if args.json:
        print(json.dumps(summary, indent=2))
    else:
        print(format_summary(summary))
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
