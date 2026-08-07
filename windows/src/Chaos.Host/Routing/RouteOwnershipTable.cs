using System.Collections.ObjectModel;
using System.Diagnostics.CodeAnalysis;
using System.Text;

namespace Chaos.Host.Routing;

/// <summary>
/// The live route-ownership manifest: an immutable, validated, longest-prefix-wins
/// lookup from request path to owning component.
/// </summary>
/// <remarks>
/// <para>
/// Built once at startup by <see cref="Build"/> from the compiled-in
/// <see cref="RouteOwnershipDefaults.Manifest"/> merged with node-local
/// configuration, and registered as a singleton. It is the single authority the
/// proxy middleware consults; ASP.NET Core routing precedence never gets a vote
/// on ownership, because two sources of truth for "who serves this" is exactly
/// the ambiguity this table exists to remove.
/// </para>
/// </remarks>
public sealed class RouteOwnershipTable
{
    private readonly RouteOwnershipEntry[] _byDescendingPrefixLength;

    private RouteOwnershipTable(IReadOnlyList<RouteOwnershipEntry> entries)
    {
        Entries = new ReadOnlyCollection<RouteOwnershipEntry>([.. entries]);

        // Longest prefix first. Ties are impossible: Build() rejects duplicate
        // prefixes, and two different prefixes of the same length cannot both
        // match one path at a segment boundary.
        _byDescendingPrefixLength = [.. entries.OrderByDescending(e => e.PathPrefix.Length)];
    }

    /// <summary>
    /// Every row, ordered by path prefix for stable presentation on
    /// <c>GET /host/routes</c>.
    /// </summary>
    public IReadOnlyList<RouteOwnershipEntry> Entries { get; }

    /// <summary>Rows owned by the Python backend.</summary>
    public int PythonCount => Entries.Count(e => e.Owner == RouteOwner.Python);

    /// <summary>Rows served natively by this .NET host.</summary>
    public int DotnetCount => Entries.Count(e => e.Owner == RouteOwner.Dotnet);

    /// <summary>
    /// Builds a validated table from the compiled-in manifest and any
    /// configured overrides.
    /// </summary>
    /// <param name="defaults">
    /// The compiled-in manifest, normally <see cref="RouteOwnershipDefaults.Manifest"/>.
    /// </param>
    /// <param name="configured">
    /// Rows from configuration. A row whose prefix matches a default replaces
    /// that default (recorded as <see cref="RouteOwnershipSource.ConfigurationOverride"/>);
    /// a row with a new prefix is added.
    /// </param>
    /// <param name="replaceDefaults">
    /// When true, <paramref name="defaults"/> is ignored entirely and only
    /// <paramref name="configured"/> is used. For a node that wants to state the
    /// whole manifest explicitly.
    /// </param>
    /// <returns>The built table.</returns>
    /// <exception cref="RouteOwnershipException">
    /// A prefix is empty, does not start with <c>/</c>, is <c>/</c> itself,
    /// contains a query or fragment, or is declared twice within one source.
    /// </exception>
    public static RouteOwnershipTable Build(
        IReadOnlyList<RouteOwnership> defaults,
        IReadOnlyList<RouteOwnership>? configured = null,
        bool replaceDefaults = false)
    {
        ArgumentNullException.ThrowIfNull(defaults);

        var merged = new Dictionary<string, RouteOwnershipEntry>(StringComparer.OrdinalIgnoreCase);

        if (!replaceDefaults)
        {
            foreach (var route in defaults)
            {
                var normalized = Validate(route, "the compiled-in manifest");
                if (!merged.TryAdd(normalized.PathPrefix, new RouteOwnershipEntry(normalized, RouteOwnershipSource.Default)))
                {
                    throw new RouteOwnershipException(
                        $"The compiled-in route-ownership manifest declares '{normalized.PathPrefix}' more than once. "
                      + "A prefix must have exactly one owner.");
                }
            }
        }

        var seenInConfiguration = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
        foreach (var route in configured ?? [])
        {
            var normalized = Validate(route, "configuration (Chaos:Routes)");
            if (!seenInConfiguration.Add(normalized.PathPrefix))
            {
                throw new RouteOwnershipException(
                    $"Configuration declares route prefix '{normalized.PathPrefix}' more than once under Chaos:Routes. "
                  + "A prefix must have exactly one owner.");
            }

            var source = merged.ContainsKey(normalized.PathPrefix)
                ? RouteOwnershipSource.ConfigurationOverride
                : RouteOwnershipSource.Configuration;
            merged[normalized.PathPrefix] = new RouteOwnershipEntry(normalized, source);
        }

        if (merged.Count == 0)
        {
            throw new RouteOwnershipException(
                "The route-ownership manifest is empty. With no manifest the gateway would proxy nothing "
              + "and the platform API would be unreachable; refusing to start is the safer failure. "
              + "Set Chaos:ReplaceDefaultRoutes to false, or supply Chaos:Routes.");
        }

        var ordered = merged.Values
            .OrderBy(e => e.PathPrefix, StringComparer.OrdinalIgnoreCase)
            .ToList();

        return new RouteOwnershipTable(ordered);
    }

    /// <summary>
    /// Finds the owner of a request path using longest-prefix-wins matching at
    /// segment boundaries.
    /// </summary>
    /// <param name="path">
    /// An absolute request path such as <c>/api/v1/alarms/definitions</c>. The
    /// query string must not be included.
    /// </param>
    /// <returns>
    /// The most specific matching entry, or <see langword="null"/> when no row
    /// claims the path. An unclaimed path is <b>not</b> proxied — it falls
    /// through to the local pipeline (static files, then 404).
    /// </returns>
    public RouteOwnershipEntry? Match(string? path)
    {
        if (string.IsNullOrEmpty(path))
        {
            return null;
        }

        foreach (var entry in _byDescendingPrefixLength)
        {
            if (IsUnderPrefix(path, entry.PathPrefix))
            {
                return entry;
            }
        }

        return null;
    }

    /// <summary>
    /// True when <paramref name="path"/> is the prefix itself or lies beneath it
    /// at a segment boundary.
    /// </summary>
    /// <param name="path">The candidate path.</param>
    /// <param name="prefix">A normalised prefix.</param>
    /// <returns>Whether the path is covered by the prefix.</returns>
    /// <remarks>
    /// <c>/api/v1/alarms</c> covers <c>/api/v1/alarms</c>, <c>/api/v1/alarms/</c>
    /// and <c>/api/v1/alarms/active</c>, but not <c>/api/v1/alarmsdefinitions</c>.
    /// Comparison is <see cref="StringComparison.OrdinalIgnoreCase"/> so a
    /// case-flipped URL cannot escape an ownership decision.
    /// </remarks>
    public static bool IsUnderPrefix(string path, string prefix)
    {
        ArgumentNullException.ThrowIfNull(path);
        ArgumentNullException.ThrowIfNull(prefix);

        if (prefix.Length == 0 || !path.StartsWith(prefix, StringComparison.OrdinalIgnoreCase))
        {
            return false;
        }

        return path.Length == prefix.Length || path[prefix.Length] == '/';
    }

    /// <summary>Renders the table as aligned text, for log output and diagnostics.</summary>
    /// <returns>A multi-line description of every row.</returns>
    public string ToDisplayString()
    {
        var width = Entries.Count == 0 ? 0 : Entries.Max(e => e.PathPrefix.Length);
        var builder = new StringBuilder();
        foreach (var entry in Entries)
        {
            builder.Append("  ")
                   .Append(entry.PathPrefix.PadRight(width))
                   .Append("  -> ")
                   .Append(entry.Owner)
                   .Append(entry.Source == RouteOwnershipSource.Default ? string.Empty : $" ({entry.Source})")
                   .AppendLine();
        }

        return builder.ToString();
    }

    private static RouteOwnership Validate(RouteOwnership route, string origin)
    {
        ArgumentNullException.ThrowIfNull(route);

        var normalized = route.Normalized();
        var prefix = normalized.PathPrefix;

        if (string.IsNullOrEmpty(prefix))
        {
            throw new RouteOwnershipException(
                $"A route-ownership entry from {origin} has an empty PathPrefix. "
              + "Every entry must declare the absolute path prefix it governs, for example '/api/v1/alarms'.");
        }

        if (!prefix.StartsWith('/'))
        {
            throw new RouteOwnershipException(
                $"Route-ownership prefix '{prefix}' from {origin} must start with '/'.");
        }

        if (prefix == "/")
        {
            throw new RouteOwnershipException(
                "Route-ownership prefix '/' from " + origin + " is not allowed. The manifest governs API "
              + "routes; the site root serves the operator console and is handled by the static-file pipeline.");
        }

        if (prefix.AsSpan().IndexOfAny('?', '#', ' ') >= 0)
        {
            throw new RouteOwnershipException(
                $"Route-ownership prefix '{prefix}' from {origin} must be a path only - no query string, "
              + "fragment or whitespace.");
        }

        if (!Enum.IsDefined(normalized.Owner))
        {
            throw new RouteOwnershipException(
                $"Route-ownership prefix '{prefix}' from {origin} has an unrecognised Owner value "
              + $"'{normalized.Owner}'. Valid values are '{nameof(RouteOwner.Python)}' and '{nameof(RouteOwner.Dotnet)}'.");
        }

        return normalized;
    }

    /// <summary>
    /// True when the table claims <paramref name="path"/> for the given owner.
    /// </summary>
    /// <param name="path">An absolute request path.</param>
    /// <param name="owner">The owner to test for.</param>
    /// <param name="entry">The matched entry, when this returns true.</param>
    /// <returns>Whether the path is owned by <paramref name="owner"/>.</returns>
    public bool IsOwnedBy(string? path, RouteOwner owner, [NotNullWhen(true)] out RouteOwnershipEntry? entry)
    {
        entry = Match(path);
        return entry is not null && entry.Owner == owner;
    }
}
