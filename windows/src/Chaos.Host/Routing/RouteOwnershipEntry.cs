namespace Chaos.Host.Routing;

/// <summary>
/// A <see cref="RouteOwnership"/> row as it exists in a built
/// <see cref="RouteOwnershipTable"/>, together with where it came from.
/// </summary>
/// <param name="Route">The ownership declaration, with a normalised prefix.</param>
/// <param name="Source">Whether this row is the shipped default or came from configuration.</param>
public sealed record RouteOwnershipEntry(RouteOwnership Route, RouteOwnershipSource Source)
{
    /// <summary>The normalised path prefix this entry governs.</summary>
    public string PathPrefix => Route.PathPrefix;

    /// <summary>Which component serves requests under <see cref="PathPrefix"/>.</summary>
    public RouteOwner Owner => Route.Owner;

    /// <summary>The Chaos.Host version this prefix was ported in, or null while still Python-served.</summary>
    public string? PortedInVersion => Route.PortedInVersion;

    /// <summary>Free-text note carried from the manifest.</summary>
    public string? Notes => Route.Notes;

    /// <summary>
    /// Number of path segments in <see cref="PathPrefix"/>. Longest-prefix-wins
    /// matching orders by prefix length, and this is exposed because it is the
    /// first thing anyone checks when a match looks wrong.
    /// </summary>
    public int SegmentCount => PathPrefix.Count(c => c == '/');
}
