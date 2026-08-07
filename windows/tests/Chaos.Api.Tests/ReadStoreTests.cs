using Chaos.Api.Annunciator;
using Chaos.Api.Data;
using Microsoft.Data.Sqlite;

namespace Chaos.Api.Tests;

/// <summary>
/// The ADO.NET read store against a real SQLite file carrying the platform's
/// column layout.
/// </summary>
/// <remarks>
/// <para>
/// These tests prove the hand-written SQL runs and maps correctly, and — more
/// importantly — that the connection the API uses cannot write. Cross-checking
/// the SQL against what SQLAlchemy actually creates is the conformance harness's
/// job (<c>tests/test_dotnet_conformance.py</c>), which builds the schema with
/// SQLAlchemy itself rather than with the DDL below.
/// </para>
/// <para>
/// The DDL here is a deliberately minimal stand-in: only the columns this API
/// reads, with the types SQLAlchemy emits for them. If it drifts from the real
/// schema these tests keep passing and the conformance harness fails, which is
/// the correct division of labour — a unit test cannot be the authority on a
/// schema another system owns.
/// </para>
/// </remarks>
public sealed class ReadStoreTests : IDisposable
{
    private readonly string _directory;
    private readonly string _databasePath;

    public ReadStoreTests()
    {
        _directory = Directory.CreateTempSubdirectory("chaos-api-tests-").FullName;
        _databasePath = Path.Combine(_directory, "homestead.db");
        CreateSchemaAndSeed(_databasePath);
    }

    public void Dispose()
    {
        // Microsoft.Data.Sqlite pools connections; without this the file stays
        // locked on Windows and the temp directory cannot be removed.
        SqliteConnection.ClearAllPools();
        try
        {
            Directory.Delete(_directory, recursive: true);
        }
        catch (IOException)
        {
            // A leaked temp directory is not worth failing a test run over.
        }
    }

    [Fact]
    public async Task TheStoreReadsDefinitionsPointsAssetsAndOpenAlarms()
    {
        var snapshot = await ReadAsync(includePending: false);

        Assert.Equal(3, snapshot.Definitions.Count);
        Assert.Contains("energy.battery_bank.power_container.01/state_of_charge_pct", snapshot.KnownPointIds);
        Assert.Equal(2, snapshot.AssetIdsByClass["inverter"].Count);

        // The active one, plus the cleared one that is always in scope so its
        // tile shows ringback rather than going dark.
        Assert.Equal(
            ["alarm-active", "alarm-cleared"],
            snapshot.OpenAlarms.Select(alarm => alarm.Id).Order(StringComparer.Ordinal));
    }

    [Fact]
    public async Task DefinitionsComeBackOrderedByAlarmKey()
    {
        var snapshot = await ReadAsync(includePending: false);

        Assert.Equal(
            ["battery_cell_imbalance", "battery_soc_low", "inverter_fault"],
            snapshot.Definitions.Select(definition => definition.AlarmKey));
    }

    [Fact]
    public async Task SqliteIntegerBooleansMapToClrBooleans()
    {
        var snapshot = await ReadAsync(includePending: false);

        Assert.All(snapshot.Definitions, definition => Assert.True(definition.Enabled));
        Assert.False(snapshot.OpenAlarms[0].Suppressed);
    }

    [Fact]
    public async Task TimestampsFromSqliteAreNaiveBecauseTheColumnCarriesNoOffset()
    {
        var snapshot = await ReadAsync(includePending: false);

        Assert.False(snapshot.OpenAlarms[0].DetectedAt.HasOffset);
        Assert.Equal("2026-08-07T16:31:07.935433", snapshot.OpenAlarms[0].DetectedAt.ToPlatformString());
    }

    [Fact]
    public async Task PendingAlarmsAreOutOfScopeUnlessAskedFor()
    {
        Assert.DoesNotContain(
            (await ReadAsync(includePending: false)).OpenAlarms,
            alarm => alarm.State == "detected");

        Assert.Contains(
            (await ReadAsync(includePending: true)).OpenAlarms,
            alarm => alarm.State == "detected");
    }

    [Fact]
    public async Task ClearedAlarmsAreAlwaysInScopeSoRingbackSurvives()
    {
        // A cleared alarm the operator has not reset must keep its tile out of
        // the dark, whichever way include_pending is set.
        foreach (var includePending in new[] { false, true })
        {
            Assert.Contains(
                (await ReadAsync(includePending)).OpenAlarms,
                alarm => alarm.State == "cleared");
        }
    }

    [Fact]
    public async Task ReviewedAlarmsAreNeverInScope()
    {
        foreach (var includePending in new[] { false, true })
        {
            Assert.DoesNotContain(
                (await ReadAsync(includePending)).OpenAlarms,
                alarm => alarm.State == "reviewed");
        }
    }

    [Fact]
    public async Task TheEndToEndPanelReportsTheUnreachableTriggerPointOutOfService()
    {
        var snapshot = await ReadAsync(includePending: false);
        var panel = AnnunciatorPanelBuilder.Build(
            snapshot,
            PlatformTimestamp.Utc(DateTime.UtcNow));

        var tiles = panel.Bays.SelectMany(bay => bay.Tiles).ToDictionary(tile => tile.AlarmKey, StringComparer.Ordinal);

        Assert.Equal("out_of_service", tiles["battery_cell_imbalance"].State);
        Assert.False(tiles["battery_cell_imbalance"].Serviceable);

        // Same asset, same bay, a trigger point that does exist and an alarm
        // standing against it. The contrast is the point: one tile can light
        // and does, the other never could.
        Assert.Equal("alarm", tiles["battery_soc_low"].State);
        Assert.True(tiles["battery_soc_low"].Serviceable);
    }

    [Fact]
    public void TheConnectionTheApiUsesCannotWrite()
    {
        // The strongest statement available during coexistence: not "we do not
        // write", but "SQLite refuses". Python owns this schema.
        var factory = new SqliteReadOnlyConnectionFactory(_databasePath);
        using var connection = factory.OpenReadOnly();
        using var command = connection.CreateCommand();
        command.CommandText = "UPDATE alarm_definitions SET enabled = 0";

        var failure = Assert.Throws<SqliteException>(() => command.ExecuteNonQuery());
        Assert.Contains("readonly", failure.Message, StringComparison.OrdinalIgnoreCase);
    }

    [Fact]
    public async Task AMissingColumnFailsLoudlyAndNamesTheDatabase()
    {
        var brokenPath = Path.Combine(_directory, "broken.db");
        await using (var connection = new SqliteConnection($"Data Source={brokenPath}"))
        {
            await connection.OpenAsync();
            await using var command = connection.CreateCommand();
            command.CommandText = "CREATE TABLE alarm_definitions (alarm_key TEXT PRIMARY KEY)";
            await command.ExecuteNonQueryAsync();
        }

        var store = new AnnunciatorReadStore(new SqliteReadOnlyConnectionFactory(brokenPath));

        var failure = await Assert.ThrowsAsync<ChaosDataException>(
            () => store.ReadAsync(includePending: false, CancellationToken.None));

        Assert.Contains("sqlite (read-only)", failure.Message, StringComparison.Ordinal);
    }

    private Task<AnnunciatorSnapshot> ReadAsync(bool includePending) =>
        new AnnunciatorReadStore(new SqliteReadOnlyConnectionFactory(_databasePath))
            .ReadAsync(includePending, CancellationToken.None);

    /// <summary>
    /// Minimal stand-in for the columns this API reads, with the types
    /// SQLAlchemy emits for them on SQLite.
    /// </summary>
    private static void CreateSchemaAndSeed(string path)
    {
        using var connection = new SqliteConnection($"Data Source={path}");
        connection.Open();
        using var command = connection.CreateCommand();
        command.CommandText = """
            CREATE TABLE assets (
                asset_id VARCHAR(160) NOT NULL PRIMARY KEY,
                asset_class VARCHAR(80) NOT NULL
            );

            CREATE TABLE points (
                point_id VARCHAR(220) NOT NULL PRIMARY KEY,
                asset_id VARCHAR(160) NOT NULL,
                point_name VARCHAR(120) NOT NULL
            );

            CREATE TABLE alarm_definitions (
                alarm_key VARCHAR(120) NOT NULL PRIMARY KEY,
                name VARCHAR(240) NOT NULL,
                severity VARCHAR(20) NOT NULL,
                domain VARCHAR(30),
                point_name VARCHAR(120),
                asset_id VARCHAR(160),
                asset_class VARCHAR(80),
                trigger_expression TEXT,
                requires_manual_reset BOOLEAN,
                enabled BOOLEAN,
                notes JSON
            );

            CREATE TABLE alarms (
                id VARCHAR(36) NOT NULL PRIMARY KEY,
                alarm_key VARCHAR(120) NOT NULL,
                state VARCHAR(24) NOT NULL,
                suppressed BOOLEAN,
                suppression_reason VARCHAR(200),
                message TEXT,
                incident_id VARCHAR(36),
                detected_at DATETIME NOT NULL,
                activated_at DATETIME
            );

            INSERT INTO assets VALUES
                ('energy.battery_bank.power_container.01', 'battery_bank'),
                ('energy.inverter.power_container.01', 'inverter'),
                ('energy.inverter.power_container.02', 'inverter');

            INSERT INTO points VALUES
                ('energy.battery_bank.power_container.01/state_of_charge_pct',
                 'energy.battery_bank.power_container.01', 'state_of_charge_pct'),
                ('energy.inverter.power_container.01/fault_active',
                 'energy.inverter.power_container.01', 'fault_active');

            INSERT INTO alarm_definitions VALUES
                ('battery_soc_low', 'Battery state of charge low', 'warning', 'energy',
                 'state_of_charge_pct', 'energy.battery_bank.power_container.01', NULL, NULL, 0, 1,
                 '[{"meta": {"threshold_status": "commissioning_default"}}]'),
                ('battery_cell_imbalance', 'Battery cell voltage imbalance', 'warning', 'energy',
                 'cell_voltage_delta_mv', 'energy.battery_bank.power_container.01', NULL, NULL, 0, 1,
                 '[{"meta": {"threshold_status": "commissioning_default"}}]'),
                ('inverter_fault', 'Hybrid inverter fault', 'major', 'energy',
                 'fault_active', NULL, 'inverter', NULL, 0, 1, '[]');

            INSERT INTO alarms VALUES
                ('alarm-active', 'battery_soc_low', 'active', 0, NULL, 'seeded', NULL,
                 '2026-08-07 16:31:07.935433', '2026-08-07 16:31:07.935433'),
                ('alarm-pending', 'inverter_fault', 'detected', 0, NULL, 'seeded', NULL,
                 '2026-08-07 16:30:00.000000', NULL),
                ('alarm-cleared', 'inverter_fault', 'cleared', 0, NULL, 'seeded', NULL,
                 '2026-08-07 16:29:00.000000', NULL),
                ('alarm-reviewed', 'inverter_fault', 'reviewed', 0, NULL, 'seeded', NULL,
                 '2026-08-07 16:28:00.000000', NULL);
            """;
        command.ExecuteNonQuery();
    }
}
