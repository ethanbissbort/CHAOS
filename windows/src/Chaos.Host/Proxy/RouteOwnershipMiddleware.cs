using Chaos.Host.Routing;
using Microsoft.AspNetCore.Http;

namespace Chaos.Host.Proxy;

/// <summary>
/// The gateway's fork in the road: consults the route-ownership manifest and
/// either proxies the request to Python or lets it fall through to this host.
/// </summary>
/// <remarks>
/// <para>
/// Runs before static files and before endpoint execution. A
/// <see cref="RouteOwner.Python"/> match is terminal — nothing downstream sees
/// the request. A <see cref="RouteOwner.Dotnet"/> match, or no match at all,
/// continues down the pipeline.
/// </para>
/// <para>
/// The manifest is consulted directly rather than inferring ownership from
/// which endpoint ASP.NET Core happened to select. That means a .NET endpoint
/// accidentally registered under a Python-owned prefix is shadowed by the proxy
/// rather than silently stealing traffic: ownership only moves when someone
/// changes the manifest, which is the property the whole design exists to
/// provide.
/// </para>
/// <para>
/// Every response carries <c>X-Chaos-Route-Owner</c> and, when a row matched,
/// <c>X-Chaos-Route-Prefix</c>. During a migration, being able to see which
/// component served a response from the client side is worth the two headers.
/// </para>
/// </remarks>
internal sealed class RouteOwnershipMiddleware
{
    /// <summary>Response header naming the component that served the request.</summary>
    public const string OwnerHeader = "X-Chaos-Route-Owner";

    /// <summary>Response header naming the manifest prefix that matched.</summary>
    public const string PrefixHeader = "X-Chaos-Route-Prefix";

    private readonly RequestDelegate _next;
    private readonly RouteOwnershipTable _table;
    private readonly BackendForwarder _forwarder;

    public RouteOwnershipMiddleware(RequestDelegate next, RouteOwnershipTable table, BackendForwarder forwarder)
    {
        _next = next;
        _table = table;
        _forwarder = forwarder;
    }

    public Task InvokeAsync(HttpContext context)
    {
        var entry = _table.Match(context.Request.Path.Value);

        if (entry is null)
        {
            // Unclaimed: the operator console and anything else served locally.
            // Deliberately not proxied - the manifest, not a fallback rule,
            // decides what reaches the backend.
            Annotate(context, "unclaimed", prefix: null);
            return _next(context);
        }

        Annotate(context, entry.Owner == RouteOwner.Python ? "python" : "dotnet", entry.PathPrefix);

        return entry.Owner == RouteOwner.Python
            ? _forwarder.ForwardAsync(context, entry)
            : _next(context);
    }

    private static void Annotate(HttpContext context, string owner, string? prefix)
    {
        context.Response.OnStarting(static state =>
        {
            var (response, ownerValue, prefixValue) = ((HttpResponse, string, string?))state;
            response.Headers[OwnerHeader] = ownerValue;
            if (prefixValue is not null)
            {
                response.Headers[PrefixHeader] = prefixValue;
            }

            return Task.CompletedTask;
        }, (context.Response, owner, prefix));
    }
}
