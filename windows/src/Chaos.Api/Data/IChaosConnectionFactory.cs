using System.Data.Common;
using Microsoft.Data.Sqlite;

namespace Chaos.Api.Data;

/// <summary>
/// Opens connections to the platform database that the Python side owns.
/// </summary>
/// <remarks>
/// <para>
/// Every connection this factory hands out must be READ-ONLY, and read-only in
/// a way the database enforces rather than a way the code promises. During the
/// migration two implementations share one schema for months; the .NET side is
/// the newcomer and it has no business writing anything Python's SQLAlchemy
/// models own. A connection that physically cannot issue DML or DDL is a much
/// stronger statement than a code review.
/// </para>
/// <para>
/// The abstraction is <see cref="DbConnection"/> rather than a specific
/// provider so the deployment target (PostgreSQL) is reached by registering a
/// different factory and changing no SQL. See <see cref="SqliteReadOnlyConnectionFactory"/>
/// and the PostgreSQL note on it.
/// </para>
/// </remarks>
public interface IChaosConnectionFactory
{
    /// <summary>
    /// Opens a connection that cannot write. Callers dispose it.
    /// </summary>
    DbConnection OpenReadOnly();

    /// <summary>
    /// Human-readable description of what is being read, for logs and errors.
    /// Must never contain credentials.
    /// </summary>
    string Describe();
}

/// <summary>
/// Read-only SQLite access to the platform database.
/// </summary>
/// <remarks>
/// <para>
/// SQLite is the backend for CI, for a developer laptop, and for the secondary
/// control node (README: "SQLite is a supported backend"), so it is the one
/// this leg can actually test. <c>Mode=ReadOnly</c> is applied to the connection
/// string and cannot be overridden by the caller: SQLite itself rejects the
/// write, so a bug in this assembly cannot corrupt the registry.
/// </para>
/// <para>
/// For the PostgreSQL deployment target the host registers its own
/// <see cref="IChaosConnectionFactory"/> returning an <c>NpgsqlConnection</c>,
/// ideally as a role with no write grants, or with
/// <c>default_transaction_read_only=on</c> in the connection string. Npgsql is
/// deliberately not a dependency of this project: shipping an untested provider
/// would be claiming coverage this repository does not have.
/// </para>
/// </remarks>
public sealed class SqliteReadOnlyConnectionFactory : IChaosConnectionFactory
{
    private readonly string _connectionString;
    private readonly string _description;

    /// <summary>Opens the SQLite database file at <paramref name="databasePath"/> read-only.</summary>
    public SqliteReadOnlyConnectionFactory(string databasePath)
    {
        ArgumentException.ThrowIfNullOrWhiteSpace(databasePath);

        var full = Path.GetFullPath(databasePath);
        _connectionString = new SqliteConnectionStringBuilder
        {
            DataSource = full,
            Mode = SqliteOpenMode.ReadOnly,
            // The Python side runs with journal_mode=WAL (see db.py), so a
            // reader never blocks the writer and never sees a torn write.
            Cache = SqliteCacheMode.Shared,
        }.ToString();
        _description = $"sqlite (read-only) {full}";
    }

    /// <inheritdoc />
    public DbConnection OpenReadOnly()
    {
        var connection = new SqliteConnection(_connectionString);
        connection.Open();
        return connection;
    }

    /// <inheritdoc />
    public string Describe() => _description;
}
