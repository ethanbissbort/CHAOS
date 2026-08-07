using Chaos.Api.Annunciator;
using Chaos.Api.Data;

namespace Chaos.Api.Tests;

/// <summary>
/// Panel assembly: bay order, tile order, the summary counts and the horn.
/// </summary>
public sealed class PanelBuilderTests
{
    private static readonly PlatformTimestamp Now =
        PlatformTimestamp.Utc(new DateTime(2026, 8, 7, 12, 0, 0, DateTimeKind.Utc));

    private static AlarmDefinitionRow Definition(
        string key,
        string domain = "energy",
        string severity = "major",
        string? pointName = "p",
        string? assetId = "asset.a.b.01",
        string? notesJson = null) =>
        new(
            AlarmKey: key,
            Name: key.Replace('_', ' '),
            Severity: severity,
            Domain: domain,
            PointName: pointName,
            AssetId: assetId,
            AssetClass: null,
            TriggerExpression: null,
            RequiresManualReset: false,
            Enabled: true,
            NotesJson: notesJson);

    private static AlarmRow Alarm(
        string key,
        string state = "active",
        string id = "alarm-1",
        bool suppressed = false,
        int detectedSecond = 0) =>
        new(
            Id: id,
            AlarmKey: key,
            State: state,
            Suppressed: suppressed,
            SuppressionReason: suppressed ? "maintenance:test" : null,
            Message: "test",
            IncidentId: null,
            DetectedAt: PlatformTimestamp.Naive(
                new DateTime(2026, 8, 7, 11, 0, detectedSecond, DateTimeKind.Unspecified)),
            ActivatedAt: null);

    private static AnnunciatorSnapshot Snapshot(
        IEnumerable<AlarmDefinitionRow> definitions,
        IEnumerable<AlarmRow>? alarms = null,
        IEnumerable<string>? points = null) =>
        new(
            [.. definitions],
            new HashSet<string>(points ?? ["asset.a.b.01/p"], StringComparer.Ordinal),
            new Dictionary<string, IReadOnlyList<string>>(StringComparer.Ordinal),
            [.. alarms ?? []]);

    // ------------------------------------------------------------ bay order --

    [Fact]
    public void BaysFollowTheDeclaredWallOrder()
    {
        var panel = AnnunciatorPanelBuilder.Build(
            Snapshot([
                Definition("c", domain: "it"),
                Definition("a", domain: "safety"),
                Definition("b", domain: "energy"),
            ]),
            Now);

        Assert.Equal(["energy", "safety", "it"], panel.Bays.Select(bay => bay.Domain));
    }

    [Fact]
    public void AnUndeclaredDomainStillGetsABayAppendedAlphabetically()
    {
        // A new domain must never silently vanish from the panel.
        var panel = AnnunciatorPanelBuilder.Build(
            Snapshot([
                Definition("z", domain: "zebra"),
                Definition("a", domain: "aardvark"),
                Definition("e", domain: "energy"),
            ]),
            Now);

        Assert.Equal(["energy", "aardvark", "zebra"], panel.Bays.Select(bay => bay.Domain));
    }

    [Fact]
    public void DeclaredBaysCarryTheirEngravedTitle()
    {
        var panel = AnnunciatorPanelBuilder.Build(Snapshot([Definition("a", domain: "it")]), Now);

        Assert.Equal("SERVER, NETWORK AND COMMS", panel.Bays[0].Title);
    }

    [Fact]
    public void AnUndeclaredBayFallsBackToTheUppercasedDomain()
    {
        var panel = AnnunciatorPanelBuilder.Build(Snapshot([Definition("a", domain: "zebra")]), Now);

        Assert.Equal("ZEBRA", panel.Bays[0].Title);
    }

    // ----------------------------------------------------------- tile order --

    [Fact]
    public void TilesSortWorstSeverityFirstThenByKey()
    {
        var panel = AnnunciatorPanelBuilder.Build(
            Snapshot([
                Definition("zulu", severity: "warning"),
                Definition("alpha", severity: "warning"),
                Definition("bravo", severity: "emergency"),
                Definition("charlie", severity: "critical"),
            ]),
            Now);

        Assert.Equal(
            ["bravo", "charlie", "alpha", "zulu"],
            panel.Bays[0].Tiles.Select(tile => tile.AlarmKey));
    }

    [Fact]
    public void AnUnknownSeveritySortsLastRatherThanCrashing()
    {
        var panel = AnnunciatorPanelBuilder.Build(
            Snapshot([
                Definition("odd", severity: "catastrophic"),
                Definition("known", severity: "info"),
            ]),
            Now);

        Assert.Equal(["known", "odd"], panel.Bays[0].Tiles.Select(tile => tile.AlarmKey));
    }

    [Fact]
    public void TilePositionsAreStableBetweenPolls()
    {
        // An engraved window does not move. If ordering drifted between polls
        // the operator's muscle memory would be worse than useless.
        var snapshot = Snapshot([
            Definition("b", severity: "major"),
            Definition("a", severity: "major"),
            Definition("c", severity: "major"),
        ]);

        var first = AnnunciatorPanelBuilder.Build(snapshot, Now);
        var second = AnnunciatorPanelBuilder.Build(snapshot, Now);

        Assert.Equal(
            first.Bays.SelectMany(bay => bay.Tiles).Select(tile => tile.AlarmKey),
            second.Bays.SelectMany(bay => bay.Tiles).Select(tile => tile.AlarmKey));
    }

    // -------------------------------------------------------------- summary --

    [Fact]
    public void EveryDefinitionGetsExactlyOneTile()
    {
        var panel = AnnunciatorPanelBuilder.Build(
            Snapshot([
                Definition("a"),
                Definition("b", domain: "it"),
                Definition("c", domain: "safety"),
            ]),
            Now);

        Assert.Equal(3, panel.Summary.Total);
        Assert.Equal(3, panel.Bays.Sum(bay => bay.Tiles.Count));
    }

    [Fact]
    public void OutOfServiceIsCountedSeparatelyFromNormal()
    {
        // "40 defined, 0 active" must not be readable as full coverage.
        var panel = AnnunciatorPanelBuilder.Build(
            Snapshot([
                Definition("reachable", pointName: "p"),
                Definition("unreachable", pointName: "missing"),
            ]),
            Now);

        Assert.Equal(1, panel.Summary.Normal);
        Assert.Equal(1, panel.Summary.OutOfService);
    }

    [Fact]
    public void AnUnacknowledgedAlarmSoundsTheHorn()
    {
        var panel = AnnunciatorPanelBuilder.Build(
            Snapshot([Definition("a")], [Alarm("a", "active")]),
            Now);

        Assert.True(panel.Summary.Horn);
        Assert.Equal("alarm", panel.Bays[0].Tiles[0].State);
    }

    [Fact]
    public void AnAcknowledgedAlarmStaysLitAndSilencesTheHorn()
    {
        var panel = AnnunciatorPanelBuilder.Build(
            Snapshot([Definition("a")], [Alarm("a", "acknowledged")]),
            Now);

        Assert.False(panel.Summary.Horn);
        Assert.Equal("acknowledged", panel.Bays[0].Tiles[0].State);
    }

    [Fact]
    public void AClearedAlarmProducesRingbackRatherThanDarkness()
    {
        // The condition is gone but the operator has not closed it out. Going
        // straight to dark would lose the fact that it happened at all.
        var panel = AnnunciatorPanelBuilder.Build(
            Snapshot([Definition("a")], [Alarm("a", "cleared")]),
            Now);

        Assert.Equal("ringback", panel.Bays[0].Tiles[0].State);
        Assert.True(panel.Summary.RingbackTone);
        Assert.False(panel.Summary.Horn);
    }

    [Fact]
    public void ASuppressedAlarmReadsInhibitedAndDoesNotSoundTheHorn()
    {
        var panel = AnnunciatorPanelBuilder.Build(
            Snapshot([Definition("a")], [Alarm("a", "active", suppressed: true)]),
            Now);

        Assert.Equal("inhibited", panel.Bays[0].Tiles[0].State);
        Assert.False(panel.Summary.Horn);
        Assert.Equal("maintenance:test", panel.Bays[0].Tiles[0].SuppressionReason);
    }

    // ------------------------------------------------------------ duplicates --

    [Fact]
    public void TheFirstAlarmInReadOrderWinsTheTileAndTheRestAreCounted()
    {
        // One lamp per condition. The store returns newest detected_at first.
        var panel = AnnunciatorPanelBuilder.Build(
            Snapshot(
                [Definition("a")],
                [
                    Alarm("a", id: "newest", detectedSecond: 30),
                    Alarm("a", id: "older", detectedSecond: 20),
                    Alarm("a", id: "oldest", detectedSecond: 10),
                ]),
            Now);

        var tile = panel.Bays[0].Tiles[0];
        Assert.Equal("newest", tile.AlarmId);
        Assert.Equal(2, tile.DuplicateOpenCount);
    }

    // ------------------------------------------------------------- metadata --

    [Fact]
    public void ThresholdStatusIsReadOutOfTheNotesMetaBlock()
    {
        const string Notes = """
            ["a human note", {"meta": {"threshold_status": "commissioning_default"}}]
            """;

        var panel = AnnunciatorPanelBuilder.Build(Snapshot([Definition("a", notesJson: Notes)]), Now);

        Assert.Equal("commissioning_default", panel.Bays[0].Tiles[0].ThresholdStatus?.GetValue<string>());
    }

    [Fact]
    public void ThresholdStatusIsNullWhenTheNotesCarryNoMetaBlock()
    {
        var panel = AnnunciatorPanelBuilder.Build(
            Snapshot([Definition("a", notesJson: """["just a note"]""")]),
            Now);

        Assert.Null(panel.Bays[0].Tiles[0].ThresholdStatus);
    }

    [Fact]
    public void ThresholdStatusIsNullWhenNotesAreAbsent()
    {
        var panel = AnnunciatorPanelBuilder.Build(Snapshot([Definition("a", notesJson: null)]), Now);

        Assert.Null(panel.Bays[0].Tiles[0].ThresholdStatus);
    }

    [Fact]
    public void MalformedNotesJsonIsReportedRatherThanSwallowed() =>
        Assert.Throws<ChaosDataException>(() =>
            AnnunciatorPanelBuilder.Build(Snapshot([Definition("a", notesJson: "{not json")]), Now));

    [Fact]
    public void TheTileStateVocabularyIsReturnedForClients()
    {
        var panel = AnnunciatorPanelBuilder.Build(Snapshot([Definition("a")]), Now);

        foreach (var required in new[]
                 {
                     "normal", "alarm", "acknowledged", "ringback", "inhibited", "out_of_service",
                 })
        {
            Assert.True(panel.TileStates.ContainsKey(required), required);
            Assert.False(string.IsNullOrWhiteSpace(panel.TileStates[required]));
        }
    }
}
