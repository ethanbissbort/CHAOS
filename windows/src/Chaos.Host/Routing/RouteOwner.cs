namespace Chaos.Host.Routing;

/// <summary>
/// Who serves a route: the Python platform backend, or this .NET host.
/// </summary>
/// <remarks>
/// This is the whole migration seam. Porting a subsystem to .NET means
/// changing one <see cref="RouteOwnership"/> entry from <see cref="Python"/> to
/// <see cref="Dotnet"/> — and the host refuses to start if that flip is not
/// backed by a registered endpoint.
/// </remarks>
public enum RouteOwner
{
    /// <summary>
    /// Served by the Python/FastAPI backend. The gateway reverse-proxies the
    /// request to <c>Chaos:BackendUrl</c>.
    /// </summary>
    Python = 0,

    /// <summary>
    /// Served natively by this .NET host. The gateway does not proxy; the
    /// request falls through to the ASP.NET Core endpoint pipeline.
    /// </summary>
    Dotnet = 1,
}
