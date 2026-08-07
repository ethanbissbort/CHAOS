using Chaos.Api.Annunciator;
using Chaos.Api.Data;

namespace Chaos.Api.Tests;

/// <summary>
/// The rule with a safety consequence: a tile that cannot light must never
/// present as a dark, normal tile.
/// </summary>
/// <remarks>
/// <c>docs/integration-findings.md</c> F-007 is the reason this file exists. Ten
/// of the forty shipped alarms are enabled, read as healthy in the alarm list,
/// and have a trigger point that does not exist for their asset — including all
/// three SAFETY-bay alarms, covering smoke and water ingress in the 20-foot
/// container that holds the batteries, the power conversion equipment and the
/// server rack. If this logic regressed, the panel would go back to claiming
/// that quarter of the alarm set is normal.
/// </remarks>
public sealed class ServiceabilityTests
{
    private static readonly IReadOnlyDictionary<string, IReadOnlyList<string>> NoAssets =
        new Dictionary<string, IReadOnlyList<string>>(StringComparer.Ordinal);

    private static AlarmDefinitionRow Definition(
        string key = "test_alarm",
        string? pointName = "some_point",
        string? assetId = null,
        string? assetClass = null,
        string? triggerExpression = null,
        bool enabled = true) =>
        new(
            AlarmKey: key,
            Name: "Test alarm",
            Severity: "major",
            Domain: "energy",
            PointName: pointName,
            AssetId: assetId,
            AssetClass: assetClass,
            TriggerExpression: triggerExpression,
            RequiresManualReset: false,
            Enabled: enabled,
            NotesJson: null);

    private static HashSet<string> Points(params string[] ids) => new(ids, StringComparer.Ordinal);

    private static Dictionary<string, IReadOnlyList<string>> Assets(
        params (string Class, string[] Ids)[] groups) =>
        groups.ToDictionary(
            group => group.Class,
            group => (IReadOnlyList<string>)group.Ids,
            StringComparer.Ordinal);

    // ------------------------------------------------------------- reachable --

    [Fact]
    public void AssetScopedAlarmWithAnExistingTriggerPointIsServiceable()
    {
        var result = Serviceability.Evaluate(
            Definition(assetId: "energy.battery_bank.power_container.01", pointName: "state_of_charge_pct"),
            Points("energy.battery_bank.power_container.01/state_of_charge_pct"),
            NoAssets);

        Assert.True(result.Serviceable);
        Assert.Null(result.Reason);
    }

    [Fact]
    public void ClassScopedAlarmIsServiceableWhenAnyOneAssetCanReachThePoint()
    {
        var result = Serviceability.Evaluate(
            Definition(assetClass: "inverter", pointName: "fault_active"),
            Points("energy.inverter.power_container.02/fault_active"),
            Assets(("inverter", [
                "energy.inverter.power_container.01",
                "energy.inverter.power_container.02",
            ])));

        // One reachable asset is enough: the alarm can raise on that one.
        Assert.True(result.Serviceable);
    }

    [Fact]
    public void ExpressionDrivenAlarmWithNoTriggerPointIsServiceable()
    {
        var result = Serviceability.Evaluate(
            Definition(pointName: null, triggerExpression: "quality_bad_count > 0"),
            Points(),
            NoAssets);

        Assert.True(result.Serviceable);
    }

    // --------------------------------------------------------- out of service --

    [Fact]
    public void UnreachableTriggerPointIsOutOfServiceNotNormal()
    {
        // The F-007 case: battery_cell_imbalance is enabled and its trigger
        // point is not reachable on the battery bank.
        var result = Serviceability.Evaluate(
            Definition(
                key: "battery_cell_imbalance",
                assetId: "energy.battery_bank.power_container.01",
                pointName: "cell_voltage_delta_mv"),
            Points("energy.battery_bank.power_container.01/state_of_charge_pct"),
            NoAssets);

        Assert.False(result.Serviceable);
        Assert.NotNull(result.Reason);
        Assert.Contains("cell_voltage_delta_mv", result.Reason, StringComparison.Ordinal);
        Assert.Contains("can never light", result.Reason, StringComparison.Ordinal);
    }

    [Fact]
    public void DisabledDefinitionIsOutOfServiceEvenWhenItsPointExists()
    {
        var result = Serviceability.Evaluate(
            Definition(assetId: "energy.battery_bank.power_container.01", pointName: "state_of_charge_pct", enabled: false),
            Points("energy.battery_bank.power_container.01/state_of_charge_pct"),
            NoAssets);

        Assert.False(result.Serviceable);
        Assert.Equal("Definition is disabled.", result.Reason);
    }

    [Fact]
    public void ClassScopedAlarmIsOutOfServiceWhenNoAssetCanReachThePoint()
    {
        var result = Serviceability.Evaluate(
            Definition(assetClass: "server", pointName: "storage_used_pct"),
            Points("it.server.rack_01.01/availability_state"),
            Assets(("server", ["it.server.rack_01.01", "it.server.rack_01.02", "it.server.rack_01.03"])));

        Assert.False(result.Serviceable);
        Assert.NotNull(result.Reason);
        Assert.Contains("does not exist on any of the 3 'server' assets", result.Reason, StringComparison.Ordinal);
    }

    [Fact]
    public void ClassScopedAlarmIsOutOfServiceWhenTheClassIsNotInTheRegister()
    {
        var result = Serviceability.Evaluate(
            Definition(assetClass: "camera", pointName: "tamper_active"),
            Points(),
            NoAssets);

        Assert.False(result.Serviceable);
        Assert.NotNull(result.Reason);
        Assert.Contains("nothing to watch", result.Reason, StringComparison.Ordinal);
    }

    [Fact]
    public void DefinitionWithNoTriggerPointAndNoExpressionIsOutOfService()
    {
        var result = Serviceability.Evaluate(
            Definition(pointName: null, triggerExpression: null),
            Points(),
            NoAssets);

        Assert.False(result.Serviceable);
        Assert.Equal("No trigger point and no trigger expression.", result.Reason);
    }

    [Fact]
    public void DefinitionWithNoScopeAtAllIsOutOfService()
    {
        var result = Serviceability.Evaluate(
            Definition(assetId: null, assetClass: null, pointName: "some_point"),
            Points("anything/some_point"),
            NoAssets);

        // Conservative on purpose: scope could not be resolved, so the tile
        // cannot claim the condition is normal.
        Assert.False(result.Serviceable);
        Assert.Equal("Definition has neither an asset nor an asset class to scope it.", result.Reason);
    }

    [Fact]
    public void EmptyStringsCountAsAbsentJustAsTheyDoInPython()
    {
        // Python tests these fields for truthiness, so "" behaves as None. A
        // C# null-only check would call this scoped and reachable.
        var result = Serviceability.Evaluate(
            Definition(assetId: string.Empty, assetClass: string.Empty, pointName: "some_point"),
            Points("/some_point"),
            NoAssets);

        Assert.False(result.Serviceable);
        Assert.Equal("Definition has neither an asset nor an asset class to scope it.", result.Reason);
    }

    [Fact]
    public void PointMatchingIsCaseSensitive()
    {
        // Ordinal comparison, matching Python's set-of-str semantics. A
        // case-insensitive match would declare an unreachable point reachable
        // and turn an out-of-service tile dark.
        var result = Serviceability.Evaluate(
            Definition(assetId: "energy.battery_bank.power_container.01", pointName: "Cell_Voltage_Delta_MV"),
            Points("energy.battery_bank.power_container.01/cell_voltage_delta_mv"),
            NoAssets);

        Assert.False(result.Serviceable);
    }

    // ------------------------------------------------------------ tile state --

    [Theory]
    [InlineData("active")]
    [InlineData("acknowledged")]
    [InlineData("cleared")]
    [InlineData("mitigated")]
    [InlineData("detected")]
    public void AnUnserviceableTileIsOutOfServiceWhateverAlarmRowExists(string state)
    {
        var alarm = new AlarmRow(
            Id: "alarm-1",
            AlarmKey: "test_alarm",
            State: state,
            Suppressed: false,
            SuppressionReason: null,
            Message: null,
            IncidentId: null,
            DetectedAt: PlatformTimestamp.Naive(new DateTime(2026, 1, 2, 3, 4, 5, DateTimeKind.Unspecified)),
            ActivatedAt: null);

        Assert.Equal("out_of_service", AnnunciatorPanelBuilder.ResolveTileState(alarm, serviceable: false));
    }

    [Fact]
    public void AServiceableTileWithNoAlarmIsNormal() =>
        Assert.Equal("normal", AnnunciatorPanelBuilder.ResolveTileState(alarm: null, serviceable: true));

    [Fact]
    public void AnUnserviceableTileWithNoAlarmIsNeverNormal() =>
        Assert.NotEqual("normal", AnnunciatorPanelBuilder.ResolveTileState(alarm: null, serviceable: false));
}
