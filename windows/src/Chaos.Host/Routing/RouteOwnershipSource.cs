namespace Chaos.Host.Routing;

/// <summary>Where a <see cref="RouteOwnership"/> entry came from.</summary>
/// <remarks>
/// Reported on <c>GET /host/routes</c> so an operator can see at a glance which
/// rows are the shipped default and which were changed by configuration on this
/// node. A migration flip that only exists in one node's <c>appsettings.json</c>
/// is a thing you want to be able to see.
/// </remarks>
public enum RouteOwnershipSource
{
    /// <summary>From the compiled-in manifest in <see cref="RouteOwnershipDefaults"/>.</summary>
    Default = 0,

    /// <summary>Added by configuration — a prefix the compiled-in manifest does not mention.</summary>
    Configuration = 1,

    /// <summary>Present in the compiled-in manifest and overridden by configuration.</summary>
    ConfigurationOverride = 2,
}
