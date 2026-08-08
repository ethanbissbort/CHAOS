namespace Chaos.Host.Configuration;

/// <summary>
/// Settings for the documentation listener, bound from <c>Chaos:Docs</c>.
/// </summary>
/// <remarks>
/// <para>
/// The documentation site is served on a <b>separate port</b> from everything
/// else this process does — 8090 by default, against the gateway's 8080. It is
/// not a path on the gateway, and that is deliberate:
/// </para>
/// <list type="bullet">
/// <item><description>
/// The manuals have to be readable during exactly the failures they describe. A
/// listener that serves nothing but files off a disk keeps answering when the
/// backend is dead, the database is unreadable and the route manifest is
/// refusing to start the gateway pipeline.
/// </description></item>
/// <item><description>
/// It carries no API, no proxy and no write path, so nothing on it can change
/// the state of the homestead. Anyone can be told "read the docs at
/// <c>:8090</c>" without also being handed the control plane.
/// </description></item>
/// </list>
/// <para>
/// Environment overrides use the usual double-underscore form:
/// <c>CHAOS_Docs__ListenUrl</c>, <c>CHAOS_Docs__Enabled</c>.
/// </para>
/// </remarks>
public sealed class DocumentationOptions
{
    /// <summary>
    /// The default binding: every interface, port 8090. LAN-reachable on
    /// purpose — the point of the offline manual is that a phone in the pump
    /// house can read it while the site is down.
    /// </summary>
    public const string DefaultListenUrl = "http://0.0.0.0:8090";

    /// <summary>
    /// Whether the documentation listener is started at all. On by default: the
    /// documentation is part of the product, not an extra. Turning it off leaves
    /// the gateway completely unaffected — nothing else reads this listener.
    /// </summary>
    public bool Enabled { get; set; } = true;

    /// <summary>
    /// Where the documentation site listens. Default
    /// <see cref="DefaultListenUrl"/>. One absolute URL; a second listener is
    /// not a second gateway and has no reason to bind more than one address.
    /// </summary>
    /// <remarks>
    /// Change the port here and the installer's firewall rule no longer matches
    /// — see <c>windows/installer/Firewall.wxs</c>, which opens the documented
    /// port on the private profile only.
    /// </remarks>
    public string ListenUrl { get; set; } = DefaultListenUrl;

    /// <summary>
    /// Where the generated site lives. Empty means "probe" — see
    /// <see cref="Docs.DocumentationRootResolver"/>: the <c>docs</c> folder
    /// inside the resolved operator-console web root first, then the
    /// repository's <c>src/chaos/web/docs</c>. Set explicitly to pin it.
    /// </summary>
    public string? RootPath { get; set; }
}
