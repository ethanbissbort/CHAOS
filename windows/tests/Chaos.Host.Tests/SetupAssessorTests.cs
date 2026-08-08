using Chaos.Host.Setup;
using Chaos.Host.Tests.Support;

namespace Chaos.Host.Tests;

/// <summary>
/// The decision table that stands between an automatic setup and a homestead's
/// alarm history.
/// </summary>
/// <remarks>
/// Every case here is a shape a real machine can be in. The ones that matter
/// most are the ones that must end <em>without</em> a verdict of
/// <see cref="SetupVerdict.SetupRequired"/>: a database with rows in it is never
/// something this gateway touches on its own.
/// </remarks>
public sealed class SetupAssessorTests
{
    private static readonly DesignPackageFacts GoodData =
        new("/repo/data", "derived", Exists: true, DocumentCount: 10, Fingerprint: "sha256:aaa", Problem: null);

    [Fact]
    public void A_fresh_machine_needs_setting_up()
    {
        var assessment = Assess(PlatformStatusJson.Fresh());

        Assert.Equal(SetupVerdict.SetupRequired, assessment.Verdict);
        Assert.Equal(SetupStepState.Absent, Step(assessment, SetupStepIds.Schema).State);

        // The count of a table that does not exist is unknown, not zero. A zero
        // would read as "checked, and there is nothing", which is a different
        // and more reassuring claim than the truth.
        var assets = Step(assessment, SetupStepIds.Assets);
        Assert.Null(assets.Count);
        Assert.Equal(SetupStepState.Absent, assets.State);
        Assert.Contains("does not exist", assets.Detail, StringComparison.Ordinal);
    }

    [Fact]
    public void An_already_loaded_machine_is_ready()
    {
        var assessment = Assess(PlatformStatusJson.Loaded());

        Assert.Equal(SetupVerdict.Ready, assessment.Verdict);
        Assert.Equal(SetupStepState.Present, Step(assessment, SetupStepIds.Schema).State);
        Assert.Equal(90, Step(assessment, SetupStepIds.Assets).Count);
        Assert.Equal(701, Step(assessment, SetupStepIds.Points).Count);
        Assert.Equal(245, Step(assessment, SetupStepIds.Bindings).Count);
        Assert.Equal(40, Step(assessment, SetupStepIds.AlarmDefinitions).Count);
        Assert.Equal(12, Step(assessment, SetupStepIds.LoadSchedule).Count);
    }

    [Fact]
    public void A_populated_database_with_a_missing_table_is_reported_not_repaired()
    {
        var assessment = Assess(PlatformStatusJson.Build(
            "sqlite:////var/chaos/homestead.db",
            missingTables: ["telemetry_samples", "ingest_dead_letters"],
            counts: new Dictionary<string, int>(StringComparer.Ordinal)
            {
                ["assets"] = 90,
                ["points"] = 701,
                ["alarm_definitions"] = 40,
                ["commands"] = 14,
            }));

        Assert.Equal(SetupVerdict.NeedsAttention, assessment.Verdict);
        Assert.Contains("Nothing has been changed", assessment.Summary, StringComparison.Ordinal);
        Assert.Equal(SetupStepState.Partial, Step(assessment, SetupStepIds.Schema).State);
        Assert.Contains("will not touch this database", Step(assessment, SetupStepIds.Schema).Detail,
            StringComparison.Ordinal);
    }

    [Fact]
    public void A_half_loaded_registry_is_reported_not_finished_off()
    {
        // Assets landed, points and alarm definitions did not: an interrupted
        // load-all. Re-running it would in fact be safe, but starting a write
        // against a database that already holds registry rows is not a decision
        // startup gets to make on its own.
        var assessment = Assess(PlatformStatusJson.Build(
            "sqlite:////var/chaos/homestead.db",
            missingTables: [],
            counts: new Dictionary<string, int>(StringComparer.Ordinal)
            {
                ["assets"] = 90,
                ["points"] = 0,
                ["alarm_definitions"] = 0,
            }));

        Assert.Equal(SetupVerdict.NeedsAttention, assessment.Verdict);
        Assert.Contains("only partly loaded", assessment.Summary, StringComparison.Ordinal);
        Assert.Contains("never drops", assessment.Summary, StringComparison.Ordinal);
    }

    [Fact]
    public void An_empty_registry_over_a_recording_database_is_left_alone()
    {
        // The schema is complete and the registry is empty, but telemetry is
        // already being written. Importing here is an operator's call.
        var assessment = Assess(PlatformStatusJson.Build(
            "sqlite:////var/chaos/homestead.db",
            missingTables: [],
            counts: new Dictionary<string, int>(StringComparer.Ordinal)
            {
                ["assets"] = 0,
                ["points"] = 0,
                ["alarm_definitions"] = 0,
                ["telemetry_samples"] = 4211,
            }));

        Assert.Equal(SetupVerdict.NeedsAttention, assessment.Verdict);
        Assert.Contains("already recording something", assessment.Summary, StringComparison.Ordinal);
    }

    [Fact]
    public void An_empty_schema_with_no_rows_is_safe_to_set_up()
    {
        // Tables exist but nothing is in them: an interrupted init-db. Nothing
        // to lose, so this one proceeds.
        var assessment = Assess(PlatformStatusJson.Build(
            "sqlite:////var/chaos/homestead.db",
            missingTables: [],
            counts: new Dictionary<string, int>(StringComparer.Ordinal)
            {
                ["assets"] = 0,
                ["points"] = 0,
                ["alarm_definitions"] = 0,
            }));

        Assert.Equal(SetupVerdict.SetupRequired, assessment.Verdict);
    }

    [Fact]
    public void An_unreachable_database_is_reported_not_worked_around()
    {
        var assessment = Assess(PlatformStatusJson.Build(
            "postgresql://chaos@db/chaos",
            missingTables: [],
            counts: new Dictionary<string, int>(StringComparer.Ordinal),
            databaseReachable: false));

        Assert.Equal(SetupVerdict.NeedsAttention, assessment.Verdict);
        Assert.Equal(SetupStepState.Absent, Step(assessment, SetupStepIds.Database).State);
    }

    [Fact]
    public void Setup_is_not_attempted_when_there_is_nothing_to_load_from()
    {
        var missingData = new DesignPackageFacts(
            "/repo/data", "configured", Exists: false, DocumentCount: 0, Fingerprint: null, Problem: null);

        var assessment = Assess(PlatformStatusJson.Fresh(), missingData);

        Assert.Equal(SetupVerdict.NeedsAttention, assessment.Verdict);
        Assert.Contains("does not exist", assessment.Summary, StringComparison.Ordinal);
        Assert.Equal(SetupStepState.Absent, Step(assessment, SetupStepIds.DataDirectory).State);
    }

    [Fact]
    public void Without_a_record_of_loading_it_the_design_package_step_is_unknown()
    {
        var assessment = Assess(PlatformStatusJson.Loaded());
        var step = Step(assessment, SetupStepIds.DesignPackage);

        Assert.Equal(SetupStepState.Unknown, step.State);
        Assert.Contains("not a claim that it is stale", step.Detail, StringComparison.Ordinal);

        // Unknown provenance never stops a loaded platform reading as ready.
        Assert.Equal(SetupVerdict.Ready, assessment.Verdict);
    }

    [Fact]
    public void A_matching_fingerprint_says_the_registry_is_in_step_with_data()
    {
        var record = new SetupRecord
        {
            CompletedUtc = DateTimeOffset.Parse("2026-08-01T09:00:00Z", null),
            DesignPackageFingerprint = GoodData.Fingerprint,
        };

        var assessment = Assess(PlatformStatusJson.Loaded(), record: record);

        Assert.Equal(SetupStepState.Present, Step(assessment, SetupStepIds.DesignPackage).State);
    }

    [Fact]
    public void A_changed_design_package_is_reported_and_not_reimported()
    {
        var record = new SetupRecord
        {
            CompletedUtc = DateTimeOffset.Parse("2026-08-01T09:00:00Z", null),
            DesignPackageFingerprint = "sha256:the-package-as-it-used-to-be",
        };

        var assessment = Assess(PlatformStatusJson.Loaded(), record: record);
        var step = Step(assessment, SetupStepIds.DesignPackage);

        Assert.Equal(SetupStepState.Partial, step.State);
        Assert.Contains("Nothing is re-imported automatically", step.Detail, StringComparison.Ordinal);

        // Still ready: an edited design package is a normal operator action, not
        // a fault, and re-importing it is their decision.
        Assert.Equal(SetupVerdict.Ready, assessment.Verdict);
    }

    [Fact]
    public void Every_step_is_reported_in_the_documented_order()
    {
        var assessment = Assess(PlatformStatusJson.Fresh());

        Assert.Equal(SetupStepIds.Ordered, [.. assessment.Steps.Select(step => step.Id)]);
        Assert.All(assessment.Steps, step =>
            Assert.False(string.IsNullOrWhiteSpace(step.Detail), $"Step '{step.Id}' explained nothing."));
    }

    private static SetupAssessment Assess(
        string statusJson,
        DesignPackageFacts? data = null,
        SetupRecord? record = null)
    {
        Assert.True(PlatformStatusReading.TryParse(statusJson, out var reading, out var problem), problem);
        return SetupAssessor.Assess(
            reading!,
            data ?? GoodData,
            DatabaseFileFacts.Inspect(reading!.DatabaseUrl),
            record);
    }

    private static SetupStepReport Step(SetupAssessment assessment, string id) =>
        assessment.Steps.Single(step => step.Id == id);
}
