using System.Globalization;

namespace Chaos.Host.Setup;

/// <summary>What the gateway concluded about the platform database.</summary>
internal enum SetupVerdict
{
    /// <summary>Nothing conclusive. Never treated as either "fine" or "needs setup".</summary>
    Unknown = 0,

    /// <summary>Schema complete, design package loaded. Nothing to do.</summary>
    Ready = 1,

    /// <summary>Empty or absent, and safe to set up: there is no data here to lose.</summary>
    SetupRequired = 2,

    /// <summary>
    /// Present, but not in a shape the gateway will modify unattended. Reported,
    /// never repaired.
    /// </summary>
    NeedsAttention = 3,
}

/// <summary>The conclusion, with the checklist it was drawn from.</summary>
/// <param name="Verdict">What to do about it.</param>
/// <param name="Summary">One line naming the situation.</param>
/// <param name="Steps">The per-step detail, in <see cref="SetupStepIds.Ordered"/> order.</param>
internal sealed record SetupAssessment(
    SetupVerdict Verdict,
    string Summary,
    IReadOnlyList<SetupStepReport> Steps);

/// <summary>
/// Decides what state the platform is in, from what the platform itself said.
/// </summary>
/// <remarks>
/// <para>
/// Pure and side-effect free: it reads a <see cref="PlatformStatusReading"/>,
/// facts about <c>data/</c> and the gateway's own setup record, and returns a
/// verdict. That is what makes "an existing-but-wrong database is reported
/// rather than repaired" a thing a unit test can pin down.
/// </para>
/// <para>
/// <b>The rule that matters.</b> Setup is only ever performed against a
/// database with nothing in it. Any row anywhere means this database holds a
/// homestead's history, and from that point the gateway reports and stops.
/// </para>
/// </remarks>
internal static class SetupAssessor
{
    internal const string AssetsTable = "assets";
    internal const string PointsTable = "points";
    internal const string BindingsTable = "point_bindings";
    internal const string AlarmDefinitionsTable = "alarm_definitions";
    internal const string LoadScheduleTable = "power_load_profiles";

    /// <summary>Missing tables named individually before the list is summarised.</summary>
    private const int NamedMissingTables = 8;

    /// <summary>Assesses the platform.</summary>
    /// <param name="reading">What <c>status --json</c> said.</param>
    /// <param name="data">What is on disk in <c>data/</c>.</param>
    /// <param name="database">What is on disk for the database.</param>
    /// <param name="record">What this gateway remembers about its last setup, or null.</param>
    /// <returns>The assessment.</returns>
    public static SetupAssessment Assess(
        PlatformStatusReading reading,
        DesignPackageFacts data,
        DatabaseFileFacts database,
        SetupRecord? record)
    {
        ArgumentNullException.ThrowIfNull(reading);
        ArgumentNullException.ThrowIfNull(data);
        ArgumentNullException.ThrowIfNull(database);

        var assets = reading.Count(AssetsTable);
        var points = reading.Count(PointsTable);
        var alarms = reading.Count(AlarmDefinitionsTable);

        var loadedSignals = Positive(assets) + Positive(points) + Positive(alarms);
        var registryLoaded = loadedSignals == 3;
        var registryEmpty = loadedSignals == 0;
        var hasRows = reading.TotalRows > 0;

        var rows = N(reading.TotalRows);
        var missingCount = N(reading.MissingTables.Count);

        var steps = new List<SetupStepReport>
        {
            DataDirectoryStep(data),
            DatabaseStep(reading, database),
            SchemaStep(reading, hasRows),
            CountStep(SetupStepIds.Assets, "Assets", AssetsTable, assets, reading,
                "Functional positions from the asset register."),
            CountStep(SetupStepIds.Points, "Points", PointsTable, points, reading,
                "Point instances from the point dictionary."),
            CountStep(SetupStepIds.Bindings, "Point bindings", BindingsTable, reading.Count(BindingsTable), reading,
                "Vendor and protocol bindings. Most stay 'tbd' until commissioning verifies a real address."),
            CountStep(SetupStepIds.AlarmDefinitions, "Alarm definitions", AlarmDefinitionsTable, alarms, reading,
                "Alarm definitions with severity and operating context."),
            CountStep(SetupStepIds.LoadSchedule, "Load schedule", LoadScheduleTable,
                reading.Count(LoadScheduleTable), reading,
                "EMS load records with tier, control method and restoration policy."),
            DesignPackageStep(data, record),
        };

        // --- The unreachable case ------------------------------------------
        if (reading.DatabaseReachable == false)
        {
            return new SetupAssessment(
                SetupVerdict.NeedsAttention,
                "The platform could not open its database. Nothing has been changed. Check the database URL and "
              + "that the target is reachable and writable.",
                steps);
        }

        // --- Anything with rows and an incomplete schema is not ours to fix --
        if (!reading.SchemaComplete && hasRows)
        {
            return new SetupAssessment(
                SetupVerdict.NeedsAttention,
                $"This database holds {rows} row(s) but is missing {missingCount} table(s) the platform declares. "
              + "That is not a fresh install and it is not a complete one. Nothing has been changed: this database "
              + "may hold alarm and telemetry history, so the gateway reports the mismatch rather than repairing "
              + "it.",
                steps);
        }

        // --- Complete schema -----------------------------------------------
        if (reading.SchemaComplete)
        {
            if (registryLoaded)
            {
                return new SetupAssessment(
                    SetupVerdict.Ready,
                    $"The platform is set up: every declared table is present, with {Show(assets)} asset(s), "
                  + $"{Show(points)} point(s) and {Show(alarms)} alarm definition(s) loaded.",
                    steps);
            }

            if (!registryEmpty)
            {
                return new SetupAssessment(
                    SetupVerdict.NeedsAttention,
                    "The schema is complete but the design package is only partly loaded "
                  + Describe(assets, points, alarms)
                  + ". That is what an interrupted load looks like. Nothing has been changed. Re-running the load "
                  + "is safe and additive — it upserts, it never drops — but the gateway will not start it "
                  + "unattended against a database that already holds registry rows.",
                    steps);
            }

            if (hasRows)
            {
                return new SetupAssessment(
                    SetupVerdict.NeedsAttention,
                    "The schema is complete and the registry is empty, but this database already holds "
                  + $"{rows} row(s) in other tables. Loading the design package into a database that is already "
                  + "recording something is a decision for an operator, not for startup.",
                    steps);
            }

            return Required(
                "The database has every declared table but nothing is loaded into it. The design package needs "
              + "importing.",
                data,
                steps);
        }

        // --- Incomplete schema, nothing in it -------------------------------
        return Required(
            reading.MissingTables.Count == 0
                ? "The database is empty and needs setting up."
                : $"This is a fresh machine: {missingCount} declared table(s) do not exist and the database holds "
                + "no rows.",
            data,
            steps);
    }

    /// <summary>
    /// Setup is required — unless the thing it would load from is demonstrably
    /// not there, in which case running it would only produce a worse error.
    /// </summary>
    private static SetupAssessment Required(
        string summary,
        DesignPackageFacts data,
        IReadOnlyList<SetupStepReport> steps)
    {
        if (data.Directory is not null && (!data.Exists || data.DocumentCount == 0))
        {
            return new SetupAssessment(
                SetupVerdict.NeedsAttention,
                summary + " The design package cannot be loaded, though: "
              + (data.Exists
                    ? $"'{data.Directory}' holds no *.yaml design documents."
                    : $"'{data.Directory}' does not exist.")
              + " Point Chaos:DataDirectory at the platform's data/ folder, or install the design package.",
                steps);
        }

        return new SetupAssessment(SetupVerdict.SetupRequired, summary, steps);
    }

    private static SetupStepReport DataDirectoryStep(DesignPackageFacts data)
    {
        const string title = "Design package directory";

        if (data.Problem is not null)
        {
            return new SetupStepReport(SetupStepIds.DataDirectory, title, SetupStepState.Unknown, data.Problem,
                Value: data.Directory);
        }

        if (!data.Exists)
        {
            return new SetupStepReport(SetupStepIds.DataDirectory, title, SetupStepState.Absent,
                $"'{data.Directory}' does not exist ({data.Source} path). The design package is what the registry "
              + "is built from; without it there is nothing to load.",
                Value: data.Directory);
        }

        if (data.DocumentCount is 0)
        {
            return new SetupStepReport(SetupStepIds.DataDirectory, title, SetupStepState.Absent,
                $"'{data.Directory}' exists but holds no *.yaml design documents.",
                Count: 0, Value: data.Directory);
        }

        return new SetupStepReport(SetupStepIds.DataDirectory, title, SetupStepState.Present,
            $"{Show(data.DocumentCount)} design document(s) in '{data.Directory}' ({data.Source} path).",
            Count: data.DocumentCount, Value: data.Directory);
    }

    private static SetupStepReport DatabaseStep(PlatformStatusReading reading, DatabaseFileFacts database)
    {
        const string title = "Database";
        var value = database.Url ?? reading.DatabaseUrl;

        return reading.DatabaseReachable switch
        {
            true => new SetupStepReport(SetupStepIds.Database, title, SetupStepState.Present,
                "The platform opened its database. " + database.Describe(), Value: value),
            false => new SetupStepReport(SetupStepIds.Database, title, SetupStepState.Absent,
                "The platform could not open its database. " + database.Describe(), Value: value),
            _ => new SetupStepReport(SetupStepIds.Database, title, SetupStepState.Unknown,
                "The platform did not report whether it could open its database. " + database.Describe(),
                Value: value),
        };
    }

    private static SetupStepReport SchemaStep(PlatformStatusReading reading, bool hasRows)
    {
        const string title = "Schema";

        if (reading.SchemaComplete)
        {
            return new SetupStepReport(SetupStepIds.Schema, title, SetupStepState.Present,
                "Every table the platform declares is present.");
        }

        var missing = string.Join(", ", reading.MissingTables.Take(NamedMissingTables));
        var more = reading.MissingTables.Count > NamedMissingTables
            ? $" and {N(reading.MissingTables.Count - NamedMissingTables)} more"
            : string.Empty;

        if (hasRows)
        {
            return new SetupStepReport(SetupStepIds.Schema, title, SetupStepState.Partial,
                $"{N(reading.MissingTables.Count)} declared table(s) are missing ({missing}{more}) while the "
              + $"database already holds {N(reading.TotalRows)} row(s). The gateway will not touch this database.",
                Count: reading.MissingTables.Count);
        }

        return new SetupStepReport(SetupStepIds.Schema, title, SetupStepState.Absent,
            $"{N(reading.MissingTables.Count)} declared table(s) are missing ({missing}{more}). "
          + "'init-db' creates tables and never alters or drops one.",
            Count: reading.MissingTables.Count);
    }

    private static SetupStepReport CountStep(
        string id,
        string title,
        string table,
        int? count,
        PlatformStatusReading reading,
        string what)
    {
        if (count is null)
        {
            return new SetupStepReport(id, title, SetupStepState.Absent,
                $"The '{table}' table does not exist in this database, so nothing was counted. {what}");
        }

        if (count == 0)
        {
            var note = Positive(reading.Count(AssetsTable)) == 1
                ? " Other registry tables are populated, so either the design package defines none of these or "
                + "that loader was skipped (load-all runs with --skip-missing)."
                : string.Empty;

            return new SetupStepReport(id, title, SetupStepState.Absent,
                $"The table exists and holds no rows. {what}{note}", Count: 0);
        }

        return new SetupStepReport(id, title, SetupStepState.Present,
            $"{Show(count)} row(s). {what}", Count: count);
    }

    private static SetupStepReport DesignPackageStep(DesignPackageFacts data, SetupRecord? record)
    {
        const string title = "Registry in step with data/";

        if (data.Fingerprint is null)
        {
            return new SetupStepReport(SetupStepIds.DesignPackage, title, SetupStepState.Unknown,
                "The design package on disk could not be fingerprinted, so whether the registry matches it is "
              + "unknown.");
        }

        if (record?.DesignPackageFingerprint is null)
        {
            return new SetupStepReport(SetupStepIds.DesignPackage, title, SetupStepState.Unknown,
                "This gateway has no record of loading the design package on this machine, so whether the "
              + "registry matches the current contents of data/ is unknown. It is not a claim that it is stale.");
        }

        if (string.Equals(record.DesignPackageFingerprint, data.Fingerprint, StringComparison.Ordinal))
        {
            return new SetupStepReport(SetupStepIds.DesignPackage, title, SetupStepState.Present,
                "The registry was loaded by this gateway from the current contents of data/ "
              + $"({Stamp(record.CompletedUtc)}).",
                Value: data.Fingerprint);
        }

        return new SetupStepReport(SetupStepIds.DesignPackage, title, SetupStepState.Partial,
            $"data/ has changed since this gateway loaded it ({Stamp(record.CompletedUtc)}). The registry still "
          + "holds the earlier load. Nothing is re-imported automatically — POST /host/setup/run?force=true to "
          + "import the current package. The load upserts and never drops.",
            Value: data.Fingerprint);
    }

    private static string Describe(int? assets, int? points, int? alarms) =>
        $" (assets {Show(assets)}, points {Show(points)}, alarm definitions {Show(alarms)})";

    /// <summary>A count, or the words for "there is no such table" — never a substituted zero.</summary>
    private static string Show(int? value) =>
        value is null ? "no such table" : N(value.Value);

    private static string N(int value) => value.ToString(CultureInfo.InvariantCulture);

    private static string Stamp(DateTimeOffset? value) =>
        value is null ? "at an unrecorded time" : value.Value.ToString("u", CultureInfo.InvariantCulture);

    private static int Positive(int? value) => value is > 0 ? 1 : 0;
}
