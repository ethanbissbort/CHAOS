using Chaos.Api.Data;

namespace Chaos.Api.Annunciator;

/// <summary>Whether a tile is capable of lighting, and why not when it is not.</summary>
/// <param name="Serviceable">True when the condition can actually be evaluated.</param>
/// <param name="Reason">
/// Operator-facing explanation when <paramref name="Serviceable"/> is false;
/// <see langword="null"/> when it is true.
/// </param>
public readonly record struct ServiceabilityResult(bool Serviceable, string? Reason)
{
    /// <summary>The tile can light.</summary>
    public static ServiceabilityResult Ok { get; } = new(true, null);

    /// <summary>The tile cannot light, for the stated reason.</summary>
    public static ServiceabilityResult No(string reason) => new(false, reason);
}

/// <summary>
/// Decides whether an annunciator tile can ever light.
/// </summary>
/// <remarks>
/// <para>
/// This is the safety-critical part of the endpoint and the reason the panel
/// exists. A fixed grid means <b>a dark tile is a positive claim</b>: it says
/// "this condition is normal". That claim is only true if the tile is capable of
/// lighting. A definition that is disabled, or whose trigger point does not
/// exist for its asset, can never light and would sit dark and reassuring
/// forever.
/// </para>
/// <para>
/// So an unserviceable tile reports <c>out_of_service</c> rather than
/// <c>normal</c> — the software equivalent of the paper OUT OF SERVICE tag taped
/// over a window on a real panel. <c>docs/integration-findings.md</c> F-007 is
/// exactly this case: ten of the forty shipped alarms are enabled, look healthy
/// in the alarm list, and have a trigger point that does not exist for their
/// asset. All three SAFETY-bay alarms are among them.
/// </para>
/// <para>
/// The check is deliberately conservative. Every path that cannot prove the tile
/// can light returns unserviceable — including the ones that look like they
/// should never happen — because the cost of a wrong "normal" is an operator
/// trusting a dark smoke-detection window in the container that holds the
/// batteries and the server rack.
/// </para>
/// </remarks>
public static class Serviceability
{
    /// <summary>
    /// Evaluates one definition against what the registry can actually reach.
    /// </summary>
    /// <param name="definition">The alarm definition.</param>
    /// <param name="knownPointIds">Every <c>&lt;asset_id&gt;/&lt;point_name&gt;</c> in the registry.</param>
    /// <param name="assetIdsByClass">Asset IDs grouped by asset class.</param>
    public static ServiceabilityResult Evaluate(
        AlarmDefinitionRow definition,
        IReadOnlySet<string> knownPointIds,
        IReadOnlyDictionary<string, IReadOnlyList<string>> assetIdsByClass)
    {
        ArgumentNullException.ThrowIfNull(definition);
        ArgumentNullException.ThrowIfNull(knownPointIds);
        ArgumentNullException.ThrowIfNull(assetIdsByClass);

        if (!definition.Enabled)
        {
            return ServiceabilityResult.No("Definition is disabled.");
        }

        var pointName = definition.PointName;
        if (string.IsNullOrEmpty(pointName))
        {
            // Expression-driven alarms have no single trigger point; they are
            // evaluated by the engine and are serviceable by construction.
            return string.IsNullOrEmpty(definition.TriggerExpression)
                ? ServiceabilityResult.No("No trigger point and no trigger expression.")
                : ServiceabilityResult.Ok;
        }

        if (!string.IsNullOrEmpty(definition.AssetId))
        {
            var pointId = $"{definition.AssetId}/{pointName}";
            return knownPointIds.Contains(pointId)
                ? ServiceabilityResult.Ok
                : ServiceabilityResult.No(
                    $"Trigger point '{pointName}' does not exist for {definition.AssetId}. "
                    + "The condition cannot be evaluated, so this tile can never light.");
        }

        if (!string.IsNullOrEmpty(definition.AssetClass))
        {
            var candidates = assetIdsByClass.TryGetValue(definition.AssetClass, out var found)
                ? found
                : [];

            if (candidates.Count == 0)
            {
                return ServiceabilityResult.No(
                    $"No asset of class '{definition.AssetClass}' is in the register, so this "
                    + "class-scoped alarm has nothing to watch.");
            }

            var reachable = candidates.Any(assetId => knownPointIds.Contains($"{assetId}/{pointName}"));
            return reachable
                ? ServiceabilityResult.Ok
                : ServiceabilityResult.No(
                    $"Trigger point '{pointName}' does not exist on any of the "
                    + $"{candidates.Count} '{definition.AssetClass}' assets.");
        }

        return ServiceabilityResult.No("Definition has neither an asset nor an asset class to scope it.");
    }
}
