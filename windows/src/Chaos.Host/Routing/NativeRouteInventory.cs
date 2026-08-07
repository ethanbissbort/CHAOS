using Microsoft.AspNetCore.Routing;

namespace Chaos.Host.Routing;

/// <summary>
/// What this .NET host actually serves: the route patterns of every endpoint
/// registered in the application, captured once the pipeline has been built.
/// </summary>
/// <remarks>
/// Recorded separately from ASP.NET Core's <c>EndpointDataSource</c> so that
/// the ownership check has an explicit, testable input rather than depending on
/// how the framework happens to expose its routing tables.
/// </remarks>
public sealed class NativeRouteInventory
{
    private readonly List<string> _patterns = [];
    private readonly Lock _gate = new();

    /// <summary>
    /// Normalised route patterns for every registered endpoint, for example
    /// <c>/host/routes</c> or <c>/api/v1/alarms/definitions/{key}</c>.
    /// </summary>
    public IReadOnlyList<string> Patterns
    {
        get
        {
            lock (_gate)
            {
                return [.. _patterns];
            }
        }
    }

    /// <summary>Whether <see cref="Capture"/> has run.</summary>
    public bool Captured { get; private set; }

    /// <summary>
    /// Records every endpoint route pattern known to <paramref name="endpoints"/>.
    /// Call once, after all <c>Map*</c> calls.
    /// </summary>
    /// <param name="endpoints">The application's endpoint route builder.</param>
    public void Capture(IEndpointRouteBuilder endpoints)
    {
        ArgumentNullException.ThrowIfNull(endpoints);

        var patterns = endpoints.DataSources
            .SelectMany(source => source.Endpoints)
            .OfType<RouteEndpoint>()
            .Select(endpoint => Normalize(endpoint.RoutePattern.RawText))
            .Where(pattern => pattern.Length > 0);

        lock (_gate)
        {
            foreach (var pattern in patterns)
            {
                if (!_patterns.Contains(pattern, StringComparer.OrdinalIgnoreCase))
                {
                    _patterns.Add(pattern);
                }
            }

            _patterns.Sort(StringComparer.OrdinalIgnoreCase);
            Captured = true;
        }
    }

    /// <summary>
    /// Adds a route pattern by hand. For tests, and for any handler that serves
    /// a path without registering an ASP.NET Core endpoint.
    /// </summary>
    /// <param name="pattern">An absolute route pattern.</param>
    public void Add(string pattern)
    {
        var normalized = Normalize(pattern);
        if (normalized.Length == 0)
        {
            return;
        }

        lock (_gate)
        {
            if (!_patterns.Contains(normalized, StringComparer.OrdinalIgnoreCase))
            {
                _patterns.Add(normalized);
                _patterns.Sort(StringComparer.OrdinalIgnoreCase);
            }

            Captured = true;
        }
    }

    /// <summary>
    /// Route patterns that begin at or beneath <paramref name="pathPrefix"/>.
    /// </summary>
    /// <param name="pathPrefix">A normalised ownership prefix.</param>
    /// <returns>The matching patterns, possibly empty.</returns>
    public IReadOnlyList<string> PatternsUnder(string pathPrefix)
    {
        ArgumentNullException.ThrowIfNull(pathPrefix);
        return [.. Patterns.Where(pattern => RouteOwnershipTable.IsUnderPrefix(pattern, pathPrefix))];
    }

    private static string Normalize(string? rawText)
    {
        var value = (rawText ?? string.Empty).Trim();
        if (value.Length == 0)
        {
            return string.Empty;
        }

        if (!value.StartsWith('/'))
        {
            value = "/" + value;
        }

        while (value.Length > 1 && value.EndsWith('/'))
        {
            value = value[..^1];
        }

        return value;
    }
}
