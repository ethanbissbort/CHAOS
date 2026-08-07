namespace Chaos.Api;

/// <summary>
/// The <c>/api/v1</c> prefixes this assembly serves natively.
/// </summary>
/// <remarks>
/// <para>
/// This list is the migration seam, expressed as data. <c>Chaos.Host</c> builds
/// its route-ownership manifest from it rather than from a hand-copied string
/// literal, so a route can never be marked <c>Dotnet</c> in the gateway without
/// an endpoint behind it, and an endpoint can never be added here without the
/// gateway knowing to stop proxying it to Python.
/// </para>
/// <para>
/// Porting a subsystem means adding its prefix here and flipping the
/// corresponding manifest entry. Rolling back means flipping that entry to
/// <c>Python</c> — the endpoints stay registered and simply stop receiving
/// traffic, which is why rollback costs a config reload rather than a rebuild.
/// See <c>docs/dotnet-migration.md</c>.
/// </para>
/// </remarks>
public static class ChaosApiRoutes
{
    /// <summary>The API version prefix everything here hangs off.</summary>
    public const string ApiPrefix = "/api/v1";

    /// <summary>Annunciator panel (SDD 14, 17.3). Read-only.</summary>
    public const string Annunciator = ApiPrefix + "/annunciator";

    /// <summary>
    /// Every prefix served natively, in a form the gateway can compare against
    /// its manifest. Ordinal, lower-case, no trailing slash.
    /// </summary>
    public static IReadOnlyList<string> OwnedPrefixes { get; } = [Annunciator];
}
