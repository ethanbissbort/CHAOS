namespace Chaos.Host.Routing;

/// <summary>
/// One row of the route-ownership manifest: which component serves everything
/// under a given path prefix.
/// </summary>
/// <remarks>
/// <para>
/// Bound from configuration (<c>Chaos:Routes</c> in <c>appsettings.json</c>, or
/// <c>CHAOS_Routes__0__PathPrefix</c> style environment variables), merged over
/// the compiled-in default in <see cref="RouteOwnershipDefaults"/>.
/// </para>
/// <para>
/// Matching is <b>longest prefix wins</b> and respects segment boundaries, so
/// <c>/api/v1/alarms/definitions</c> can be owned by .NET while
/// <c>/api/v1/alarms</c> remains Python, and <c>/api/v1/alarmsfoo</c> matches
/// neither. Comparison is ordinal case-insensitive: a case-flipped URL must not
/// slip past an ownership decision.
/// </para>
/// </remarks>
public sealed record RouteOwnership
{
    /// <summary>
    /// The absolute path prefix this entry governs, for example
    /// <c>/api/v1/alarms</c>. Must start with <c>/</c>. A trailing slash is
    /// stripped during normalisation; <c>/</c> itself is not a legal prefix
    /// because the manifest governs API routes, not the whole site.
    /// </summary>
    public string PathPrefix { get; init; } = string.Empty;

    /// <summary>Which component serves requests under <see cref="PathPrefix"/>.</summary>
    public RouteOwner Owner { get; init; } = RouteOwner.Python;

    /// <summary>
    /// The Chaos.Host version in which this prefix was ported to .NET, or
    /// <see langword="null"/> while it is still served by Python. Reported on
    /// <c>GET /host/routes</c> so migration progress is a fact about the running
    /// system rather than a note in someone's head.
    /// </summary>
    public string? PortedInVersion { get; init; }

    /// <summary>
    /// Free-text note: why this entry exists, what still depends on Python,
    /// what to check before flipping it. Shown verbatim on <c>GET /host/routes</c>.
    /// </summary>
    public string? Notes { get; init; }

    /// <summary>
    /// Returns this entry with <see cref="PathPrefix"/> normalised: trimmed,
    /// backslashes converted, and any trailing slash removed.
    /// </summary>
    /// <returns>A normalised copy, or the same instance when nothing changed.</returns>
    public RouteOwnership Normalized()
    {
        var normalized = NormalizePrefix(PathPrefix);
        return string.Equals(normalized, PathPrefix, StringComparison.Ordinal)
            ? this
            : this with { PathPrefix = normalized };
    }

    /// <summary>
    /// Normalises a path prefix for storage and comparison: trims whitespace,
    /// converts backslashes to forward slashes, collapses a trailing slash.
    /// </summary>
    /// <param name="prefix">The raw prefix.</param>
    /// <returns>The normalised prefix. May be empty if the input was empty.</returns>
    public static string NormalizePrefix(string? prefix)
    {
        var value = (prefix ?? string.Empty).Trim().Replace('\\', '/');
        while (value.Length > 1 && value.EndsWith('/'))
        {
            value = value[..^1];
        }

        return value;
    }
}
