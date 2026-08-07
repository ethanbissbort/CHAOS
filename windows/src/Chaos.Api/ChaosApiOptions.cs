namespace Chaos.Api;

/// <summary>
/// Configuration for the natively-served slice of <c>/api/v1</c>.
/// </summary>
/// <remarks>
/// Bound from <c>Chaos:Api</c> by the host. Kept deliberately small: this
/// assembly serves ported routes, it does not own deployment policy.
/// </remarks>
public sealed class ChaosApiOptions
{
    /// <summary>
    /// Path to the SQLite platform database, when SQLite is the backend.
    /// </summary>
    /// <remarks>
    /// Ignored if the host registers its own <see cref="Data.IChaosConnectionFactory"/>
    /// — which is how PostgreSQL is reached. Opened READ-ONLY in every case; see
    /// <see cref="Data.SqliteReadOnlyConnectionFactory"/> for why that is
    /// enforced by the database rather than promised by the code.
    /// </remarks>
    public string? SqliteDatabasePath { get; set; }
}
