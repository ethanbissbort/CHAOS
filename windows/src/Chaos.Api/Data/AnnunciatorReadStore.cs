using System.Data;
using System.Data.Common;

namespace Chaos.Api.Data;

/// <summary>Reads the rows one annunciator request needs.</summary>
public interface IAnnunciatorReadStore
{
    /// <summary>
    /// Reads a consistent snapshot of definitions, points, assets and open alarms.
    /// </summary>
    /// <param name="includePending">
    /// When true, alarms still inside their on-delay window (<c>detected</c>)
    /// are in scope as well.
    /// </param>
    /// <param name="cancellationToken">Cancels the read.</param>
    Task<AnnunciatorSnapshot> ReadAsync(bool includePending, CancellationToken cancellationToken);
}

/// <summary>
/// ADO.NET implementation over the SQLAlchemy-owned schema.
/// </summary>
/// <remarks>
/// <para>
/// The SQL is hand-written, read-only and ANSI: no provider-specific syntax, no
/// ORM, no migrations. It mirrors, statement for statement, what
/// <c>src/chaos/api/routers/annunciator.py</c> issues through
/// SQLAlchemy — including the ORDER BY clauses, because ordering is part of this
/// endpoint's contract (an engraved window does not move between polls).
/// </para>
/// <para>
/// The whole read runs inside one transaction so a concurrent Python write
/// cannot produce a panel where the definition set and the alarm set disagree.
/// </para>
/// </remarks>
public sealed class AnnunciatorReadStore : IAnnunciatorReadStore
{
    /// <summary>
    /// Alarm states that count as open but not yet pending — mirrors
    /// <c>ACTIVE_STATES</c> in <c>chaos/alarms/evaluator.py</c>.
    /// </summary>
    private static readonly string[] ActiveStates = ["active", "acknowledged", "mitigated"];

    /// <summary>
    /// Mirrors <c>OPEN_STATES</c>: adds <c>detected</c>, i.e. still inside the
    /// on-delay window.
    /// </summary>
    private static readonly string[] OpenStates = ["detected", "active", "acknowledged", "mitigated"];

    /// <summary>
    /// Always in scope regardless of <c>include_pending</c>. Ringback needs
    /// alarms that returned to normal but have not been reviewed, so a tile does
    /// not go straight from lit to dark without the operator closing it out.
    /// </summary>
    private static readonly string[] AlwaysInScopeStates = ["cleared", "mitigated"];

    private readonly IChaosConnectionFactory _connections;

    /// <summary>Creates the store over a read-only connection factory.</summary>
    public AnnunciatorReadStore(IChaosConnectionFactory connections)
    {
        ArgumentNullException.ThrowIfNull(connections);
        _connections = connections;
    }

    /// <inheritdoc />
    public async Task<AnnunciatorSnapshot> ReadAsync(bool includePending, CancellationToken cancellationToken)
    {
        await using var connection = _connections.OpenReadOnly();

        // ReadCommitted is the weakest level that still gives one consistent
        // view for the whole request. On SQLite in WAL mode the reader gets a
        // snapshot; on PostgreSQL this is the default.
        await using var transaction = await connection
            .BeginTransactionAsync(IsolationLevel.ReadCommitted, cancellationToken)
            .ConfigureAwait(false);

        try
        {
            var definitions = await ReadDefinitionsAsync(connection, transaction, cancellationToken)
                .ConfigureAwait(false);
            var points = await ReadPointIdsAsync(connection, transaction, cancellationToken)
                .ConfigureAwait(false);
            var assets = await ReadAssetsByClassAsync(connection, transaction, cancellationToken)
                .ConfigureAwait(false);
            var alarms = await ReadOpenAlarmsAsync(connection, transaction, includePending, cancellationToken)
                .ConfigureAwait(false);

            return new AnnunciatorSnapshot(definitions, points, assets, alarms);
        }
        catch (DbException ex)
        {
            // Python owns this schema. A failure here usually means it changed
            // under us, so say which database and surface the provider message
            // rather than turning it into a bare 500.
            throw new ChaosDataException(
                $"Reading the annunciator snapshot from {_connections.Describe()} failed: {ex.Message}",
                ex);
        }
        finally
        {
            await transaction.RollbackAsync(cancellationToken).ConfigureAwait(false);
        }
    }

    private static async Task<IReadOnlyList<AlarmDefinitionRow>> ReadDefinitionsAsync(
        DbConnection connection,
        DbTransaction transaction,
        CancellationToken cancellationToken)
    {
        // ORDER BY alarm_key matches the Python query. Ordering in SQL rather
        // than in memory keeps both implementations on the database's collation
        // instead of two languages' idea of string comparison.
        const string Sql = """
            SELECT alarm_key, name, severity, domain, point_name, asset_id, asset_class,
                   trigger_expression, requires_manual_reset, enabled, notes
            FROM alarm_definitions
            ORDER BY alarm_key
            """;

        await using var command = CreateCommand(connection, transaction, Sql);
        await using var reader = await command.ExecuteReaderAsync(cancellationToken).ConfigureAwait(false);

        var rows = new List<AlarmDefinitionRow>();
        while (await reader.ReadAsync(cancellationToken).ConfigureAwait(false))
        {
            rows.Add(new AlarmDefinitionRow(
                AlarmKey: DbValues.GetString(reader, 0),
                Name: DbValues.GetString(reader, 1),
                Severity: DbValues.GetString(reader, 2),
                Domain: DbValues.GetStringOrNull(reader, 3),
                PointName: DbValues.GetStringOrNull(reader, 4),
                AssetId: DbValues.GetStringOrNull(reader, 5),
                AssetClass: DbValues.GetStringOrNull(reader, 6),
                TriggerExpression: DbValues.GetStringOrNull(reader, 7),
                RequiresManualReset: DbValues.GetBoolean(reader, 8),
                Enabled: DbValues.GetBoolean(reader, 9),
                NotesJson: DbValues.GetStringOrNull(reader, 10)));
        }

        return rows;
    }

    private static async Task<IReadOnlySet<string>> ReadPointIdsAsync(
        DbConnection connection,
        DbTransaction transaction,
        CancellationToken cancellationToken)
    {
        const string Sql = "SELECT point_id FROM points";

        await using var command = CreateCommand(connection, transaction, Sql);
        await using var reader = await command.ExecuteReaderAsync(cancellationToken).ConfigureAwait(false);

        // Ordinal comparison, matching Python's set semantics over str. A
        // culture-aware comparer would make 'i' and 'I' collide on some
        // locales and quietly declare an unreachable trigger point reachable,
        // which is the exact failure this endpoint exists to prevent.
        var ids = new HashSet<string>(StringComparer.Ordinal);
        while (await reader.ReadAsync(cancellationToken).ConfigureAwait(false))
        {
            ids.Add(DbValues.GetString(reader, 0));
        }

        return ids;
    }

    private static async Task<IReadOnlyDictionary<string, IReadOnlyList<string>>> ReadAssetsByClassAsync(
        DbConnection connection,
        DbTransaction transaction,
        CancellationToken cancellationToken)
    {
        const string Sql = "SELECT asset_id, asset_class FROM assets";

        await using var command = CreateCommand(connection, transaction, Sql);
        await using var reader = await command.ExecuteReaderAsync(cancellationToken).ConfigureAwait(false);

        var grouped = new Dictionary<string, List<string>>(StringComparer.Ordinal);
        while (await reader.ReadAsync(cancellationToken).ConfigureAwait(false))
        {
            var assetId = DbValues.GetString(reader, 0);
            var assetClass = DbValues.GetString(reader, 1);
            if (!grouped.TryGetValue(assetClass, out var list))
            {
                list = [];
                grouped[assetClass] = list;
            }

            list.Add(assetId);
        }

        return grouped.ToDictionary(
            pair => pair.Key,
            pair => (IReadOnlyList<string>)pair.Value,
            StringComparer.Ordinal);
    }

    private static async Task<IReadOnlyList<AlarmRow>> ReadOpenAlarmsAsync(
        DbConnection connection,
        DbTransaction transaction,
        bool includePending,
        CancellationToken cancellationToken)
    {
        var states = (includePending ? OpenStates : ActiveStates)
            .Union(AlwaysInScopeStates, StringComparer.Ordinal)
            .Order(StringComparer.Ordinal)
            .ToArray();

        var placeholders = string.Join(", ", states.Select((_, index) => $"@state{index}"));

        // ORDER BY detected_at DESC only, exactly as the Python query does.
        //
        // KNOWN NON-DETERMINISM, deliberately reproduced rather than fixed
        // here: two open alarms on the same key with an identical detected_at
        // leave "most recent wins the tile" undefined, and the two
        // implementations may pick different rows. Adding a tiebreak on this
        // side alone would make the .NET panel disagree with the Python panel,
        // which is worse. The fix belongs in the Python query, in its own
        // change, applied to both. See docs/dotnet-migration.md.
        var sql = $"""
            SELECT id, alarm_key, state, suppressed, suppression_reason, message,
                   incident_id, detected_at, activated_at
            FROM alarms
            WHERE state IN ({placeholders})
            ORDER BY detected_at DESC
            """;

        await using var command = CreateCommand(connection, transaction, sql);
        for (var index = 0; index < states.Length; index++)
        {
            var parameter = command.CreateParameter();
            parameter.ParameterName = $"@state{index}";
            parameter.Value = states[index];
            command.Parameters.Add(parameter);
        }

        await using var reader = await command.ExecuteReaderAsync(cancellationToken).ConfigureAwait(false);

        var rows = new List<AlarmRow>();
        while (await reader.ReadAsync(cancellationToken).ConfigureAwait(false))
        {
            var detectedAt = DbValues.GetTimestamp(reader, 7)
                ?? throw new ChaosDataException("alarms.detected_at is NULL but the schema declares it NOT NULL.");

            rows.Add(new AlarmRow(
                Id: DbValues.GetString(reader, 0),
                AlarmKey: DbValues.GetString(reader, 1),
                State: DbValues.GetString(reader, 2),
                Suppressed: DbValues.GetBoolean(reader, 3),
                SuppressionReason: DbValues.GetStringOrNull(reader, 4),
                Message: DbValues.GetStringOrNull(reader, 5),
                IncidentId: DbValues.GetStringOrNull(reader, 6),
                DetectedAt: detectedAt,
                ActivatedAt: DbValues.GetTimestamp(reader, 8)));
        }

        return rows;
    }

    private static DbCommand CreateCommand(DbConnection connection, DbTransaction transaction, string sql)
    {
        var command = connection.CreateCommand();
        command.Transaction = transaction;
        command.CommandText = sql;
        return command;
    }
}
