using System.Text.RegularExpressions;

namespace Chaos.Api.Annunciator;

/// <summary>
/// The engraved legends on the annunciator windows.
/// </summary>
/// <remarks>
/// Port of <c>LEGENDS</c> and <c>engrave()</c> in
/// <c>src/homestead_twin/api/routers/annunciator.py</c>. On a real panel these
/// are cut by someone who thought about what an operator needs to read at three
/// metres in bad light; they are not a mechanical transform of a sentence, which
/// is why the hand-cut table exists and the generator is only a fallback for
/// definitions added later.
/// </remarks>
public static partial class Legends
{
    /// <summary>Maximum lines in a window.</summary>
    public const int MaxLines = 3;

    /// <summary>Maximum characters per line.</summary>
    public const int Width = 12;

    /// <summary>Words dropped when compressing a definition name into a legend.</summary>
    private static readonly HashSet<string> Stopwords = new(StringComparer.Ordinal)
    {
        "a", "an", "the", "of", "for", "to", "is", "in", "on", "and", "or",
    };

    /// <summary>
    /// Applied in order, so a longer phrase wins before a shorter one inside it.
    /// </summary>
    private static readonly (string Long, string Short)[] Abbreviations =
    [
        ("STATE OF CHARGE", "SOC"),
        ("TEMPERATURE", "TEMP"),
        ("COMMUNICATIONS", "COMMS"),
        ("COMMUNICATION", "COMMS"),
        ("CONFIGURATION", "CONFIG"),
        ("UNAVAILABLE", "UNAVAIL"),
        ("UNREACHABLE", "UNREACH"),
        ("SECONDARY", "SEC"),
        ("CONTAINER", "CONTNR"),
        ("GENERATOR", "GEN"),
        ("BATTERY", "BATT"),
        ("INVERTER", "INVTR"),
        ("PERCENT", "PCT"),
        ("MAXIMUM", "MAX"),
        ("MINIMUM", "MIN"),
    ];

    /// <summary>
    /// Hand-cut legends, one per shipped alarm.
    /// </summary>
    /// <remarks>
    /// Auto-generation was tried first on the Python side and lost the operative
    /// word on 18 of the 40: "Source transfer did not complete" became
    /// "SOURCE TRANSFER DID NOT", which is worse than no legend.
    /// </remarks>
    public static readonly IReadOnlyDictionary<string, IReadOnlyList<string>> HandCut =
        new Dictionary<string, IReadOnlyList<string>>(StringComparer.Ordinal)
        {
            ["battery_soc_low"] = ["BATT SOC", "LOW"],
            ["battery_reserve_critical"] = ["BATT RESERVE", "CRITICAL"],
            ["battery_temperature_high"] = ["BATT CELL", "TEMP HIGH"],
            ["battery_cell_imbalance"] = ["BATT CELL", "IMBALANCE"],
            ["battery_charge_inhibited"] = ["BMS CHARGE", "INHIBITED"],
            ["battery_discharge_inhibited"] = ["BMS DISCHRG", "INHIBITED", "LIVE LOAD"],
            ["inverter_fault"] = ["INVERTER", "FAULT"],
            ["inverter_overload_risk"] = ["INVERTER", "OVERLOAD", "RISK"],
            ["pv_generation_underperformance"] = ["PV OUTPUT", "BELOW", "EXPECTED"],
            ["generator_start_failed"] = ["GEN START", "FAILED"],
            ["generator_fuel_low"] = ["GEN FUEL", "LOW"],
            ["transfer_failed"] = ["SOURCE XFER", "FAILED"],
            ["load_shed_failed"] = ["LOAD SHED", "NO EFFECT"],
            ["load_restore_failed"] = ["LOAD DID NOT", "RESTORE"],
            ["energy_meter_data_invalid"] = ["EMS INPUT", "BAD/STALE"],
            ["critical_load_growth"] = ["CRIT BASE", "LOAD HIGH"],
            ["ups_runtime_low"] = ["UPS RUNTIME", "LOW"],
            ["power_container_cooling_failed"] = ["BATT ZONE", "COOLING", "FAILED"],
            ["power_container_ac_bus_lost"] = ["AC BUS", "DE-ENERGIZED"],
            ["power_container_water_ingress"] = ["PWR CONTNR", "FLUID", "DETECTED"],
            ["rack_smoke_detected"] = ["RACK SMOKE", "DETECTED"],
            ["server_zone_temperature_high"] = ["SERVER ZONE", "TEMP HIGH"],
            ["secondary_control_node_unreachable"] = ["SEC CONTROL", "NODE", "UNREACHABLE"],
            ["ups_on_battery"] = ["UPS ON", "BATTERY"],
            ["pdu_overload"] = ["RACK PDU", "OVERLOAD"],
            ["rack_ats_source_lost"] = ["RACK ATS", "PRIMARY", "SOURCE LOST"],
            ["core_switch_unreachable"] = ["CORE SWITCH", "UNREACHABLE"],
            ["primary_server_unreachable"] = ["PRIMARY HOST", "UNREACHABLE"],
            ["service_unavailable"] = ["CORE SERVICE", "UNAVAILABLE"],
            ["storage_pool_degraded"] = ["ARRAY DISK", "FAILED"],
            ["storage_capacity_high"] = ["HOST STORAGE", "HIGH"],
            ["network_path_degraded"] = ["PACKET LOSS", "HIGH"],
            ["backup_overdue"] = ["BACKUP", "OVERDUE"],
            ["time_sync_drift"] = ["HOST CLOCK", "OFFSET HIGH"],
            ["rack_door_forced_open"] = ["RACK DOOR", "FORCED OPEN"],
            ["rack_door_open_extended"] = ["RACK DOOR", "LEFT OPEN"],
            ["camera_tamper"] = ["CAMERA", "TAMPER"],
            ["camera_offline"] = ["CAMERA", "OFFLINE"],
            ["alarm_beacon_unavailable"] = ["ALARM BEACON", "UNAVAILABLE"],
            ["sensor_data_invalid"] = ["SENSOR DATA", "BAD/STALE"],
        };

    /// <summary>Characters kept when normalising a name; everything else becomes a space.</summary>
    [GeneratedRegex("[^A-Za-z0-9 %/-]")]
    private static partial Regex Unprintable();

    /// <summary>
    /// Returns the engraved legend for an alarm.
    /// </summary>
    /// <param name="name">The definition's human-readable name.</param>
    /// <param name="key">The alarm key, used to look up a hand-cut legend.</param>
    /// <returns>One to <see cref="MaxLines"/> lines, each at most <see cref="Width"/> characters.</returns>
    /// <remarks>
    /// The generated fallback keeps the LAST word above all others, because that
    /// is where the meaning usually sits: "...did not complete", "...detected",
    /// "...low". A legend that drops its verb is not a shorter legend, it is a
    /// wrong one.
    /// </remarks>
    public static IReadOnlyList<string> Engrave(string? name, string? key = null)
    {
        if (key is not null && HandCut.TryGetValue(key, out var handCut))
        {
            return handCut;
        }

        var text = Unprintable().Replace(name ?? string.Empty, " ").ToUpperInvariant();
        foreach (var (longForm, shortForm) in Abbreviations)
        {
            text = text.Replace(longForm, shortForm, StringComparison.Ordinal);
        }

        var words = text.Split(' ', StringSplitOptions.RemoveEmptyEntries);
        var kept = words.Where(word => !Stopwords.Contains(word.ToLowerInvariant())).ToList();
        if (kept.Count == 0)
        {
            kept = [.. words];
        }

        if (kept.Count == 0)
        {
            return ["(UNNAMED)"];
        }

        var lines = Wrap(kept);
        if (lines.Count <= MaxLines)
        {
            return lines;
        }

        // Too long. Drop qualifiers from the middle rather than the end, so the
        // operative word survives; keep dropping until it fits.
        var head = kept.GetRange(0, kept.Count - 1);
        var tail = kept[^1];
        while (head.Count > 0 && Wrap([.. head, tail]).Count > MaxLines)
        {
            head.RemoveAt(head.Count - 1);
        }

        lines = Wrap([.. head, tail]);
        return lines.Count > MaxLines ? lines.GetRange(0, MaxLines) : lines;
    }

    /// <summary>
    /// Greedy wrap at <see cref="Width"/>. A word longer than the window gets a
    /// line of its own rather than being hyphenated.
    /// </summary>
    private static List<string> Wrap(IReadOnlyList<string> source)
    {
        var lines = new List<string>();
        var current = string.Empty;
        foreach (var word in source)
        {
            var candidate = current.Length == 0 ? word : $"{current} {word}";
            if (candidate.Length <= Width || current.Length == 0)
            {
                current = candidate;
            }
            else
            {
                lines.Add(current);
                current = word;
            }
        }

        if (current.Length > 0)
        {
            lines.Add(current);
        }

        return lines;
    }
}
