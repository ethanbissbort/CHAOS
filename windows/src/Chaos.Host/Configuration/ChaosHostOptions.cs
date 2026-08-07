using Chaos.Host.Routing;

namespace Chaos.Host.Configuration;

/// <summary>
/// Every setting the .NET gateway has.
/// </summary>
/// <remarks>
/// <para>
/// Bound from the <c>Chaos</c> section of <c>appsettings.json</c>, then
/// overridden by <c>CHAOS_</c>-prefixed environment variables at the same level.
/// <c>CHAOS_BackendUrl=http://127.0.0.1:9000</c> overrides
/// <c>Chaos:BackendUrl</c>; nested values use the standard double-underscore
/// form, for example <c>CHAOS_Routes__0__PathPrefix</c>.
/// </para>
/// </remarks>
public sealed class ChaosHostOptions
{
    /// <summary>The configuration section these options are bound from.</summary>
    public const string SectionName = "Chaos";

    /// <summary>The environment-variable prefix that overrides that section.</summary>
    public const string EnvironmentPrefix = "CHAOS_";

    /// <summary>
    /// Where the gateway listens. Default <c>http://0.0.0.0:8080</c> — the .NET
    /// host is the only listener on the LAN. Semicolon-separated for multiple
    /// bindings. Takes precedence over <c>ASPNETCORE_URLS</c>.
    /// </summary>
    public string ListenUrl { get; set; } = "http://0.0.0.0:8080";

    /// <summary>
    /// Where the Python platform backend listens. Default
    /// <c>http://127.0.0.1:8081</c>, and it must stay on loopback: the backend
    /// is never exposed to the LAN directly, which is a deliberate improvement
    /// over reaching FastAPI on 0.0.0.0:8000. A non-loopback value is rejected
    /// at startup unless <see cref="AllowNonLoopbackBackend"/> is set.
    /// </summary>
    public string BackendUrl { get; set; } = "http://127.0.0.1:8081";

    /// <summary>
    /// Where the operator console assets live. Empty means "probe" — see
    /// <see cref="Web.WebRootResolver"/>: the packaged <c>web</c> folder beside
    /// the executable first, then the repository's
    /// <c>src/chaos/web</c> for development. Set explicitly to pin it.
    /// </summary>
    public string? WebRootPath { get; set; }

    /// <summary>
    /// How long a single proxied request may take end to end, including
    /// response streaming. Default 100 seconds, matching
    /// <see cref="HttpClient"/>'s default so behaviour is not surprising.
    /// </summary>
    public TimeSpan RequestTimeout { get; set; } = TimeSpan.FromSeconds(100);

    /// <summary>
    /// How long the registered <see cref="Abstractions.IBackendSupervisor"/> is
    /// given to start the Python backend, and the window during which the
    /// backend is reported as <c>starting</c> rather than <c>down</c>.
    /// </summary>
    public TimeSpan BackendStartTimeout { get; set; } = TimeSpan.FromSeconds(60);

    /// <summary>
    /// How long graceful shutdown may take. In-flight proxied requests are
    /// drained before the backend supervisor is asked to stop.
    /// </summary>
    public TimeSpan ShutdownTimeout { get; set; } = TimeSpan.FromSeconds(30);

    /// <summary>Interval between backend health polls while the backend is reachable.</summary>
    public TimeSpan BackendHealthInterval { get; set; } = TimeSpan.FromSeconds(10);

    /// <summary>Upper bound on the exponential backoff applied while the backend is unreachable.</summary>
    public TimeSpan BackendHealthMaxBackoff { get; set; } = TimeSpan.FromSeconds(30);

    /// <summary>Per-poll timeout for the backend health probe. Kept short so a hung backend reads as down quickly.</summary>
    public TimeSpan BackendHealthTimeout { get; set; } = TimeSpan.FromSeconds(5);

    /// <summary>
    /// While the backend stays unreachable, repeat the warning at most this
    /// often. Between repeats the failures are logged at Debug. A control system
    /// that floods its own log during an outage has destroyed the record of the
    /// outage.
    /// </summary>
    public TimeSpan BackendHealthLogQuietPeriod { get; set; } = TimeSpan.FromMinutes(5);

    /// <summary>Path polled on <see cref="BackendUrl"/> to decide backend reachability.</summary>
    public string BackendHealthPath { get; set; } = "/health";

    /// <summary>
    /// Allows <see cref="BackendUrl"/> to point somewhere other than loopback.
    /// Off by default: widening that trust boundary should be a decision someone
    /// wrote down, not a side effect of a config typo.
    /// </summary>
    public bool AllowNonLoopbackBackend { get; set; }

    /// <summary>
    /// Whether to trust an inbound <c>X-Forwarded-For</c> / <c>X-Forwarded-Proto</c>
    /// and append to it. Off by default because the gateway is the LAN edge, so
    /// an inbound forwarded header is client-supplied and unverifiable; the
    /// gateway replaces it with what it can actually observe.
    /// </summary>
    public bool TrustInboundForwardedHeaders { get; set; }

    /// <summary>
    /// When true, <see cref="Routes"/> replaces the compiled-in manifest instead
    /// of being merged over it. For a node that states the whole manifest itself.
    /// </summary>
    public bool ReplaceDefaultRoutes { get; set; }

    /// <summary>
    /// Route-ownership overrides. A row whose <see cref="RouteOwnership.PathPrefix"/>
    /// matches a compiled-in row replaces it; a new prefix is added. This is the
    /// one place a subsystem's migration is recorded per node.
    /// </summary>
    public IList<RouteOwnership> Routes { get; set; } = [];

    /// <summary>
    /// Whether the gateway should refuse to proxy when the backend reports a
    /// platform version incompatible with the contract this host was built
    /// against. Off by default — see the notes on
    /// <see cref="Health.BackendHealthState"/>. Incompatibility is always
    /// reported on <c>/health</c> and <c>/host/info</c> whether or not it is enforced.
    /// </summary>
    public bool RefuseIncompatibleBackend { get; set; }

    // -- First-run setup ---------------------------------------------------
    //
    // The owner does not want to use a CLI. These settings are what makes
    // "launch the shell" the whole installation procedure, and what lets an
    // operator who manages a node by hand turn that off.

    /// <summary>
    /// Whether the gateway checks the platform database at startup and sets it
    /// up if it is empty. <b>On by default</b>, because a fresh install must be
    /// usable without anyone opening a terminal.
    /// </summary>
    /// <remarks>
    /// <para>
    /// Turn it off (<c>CHAOS_AutoSetup=false</c>) on a node an operator manages
    /// by hand: with it off the gateway neither reads nor writes the platform
    /// database on its own, and <c>GET /host/setup</c> reports
    /// <c>not_started</c> — which is a statement about the gateway, not a claim
    /// that the database is missing.
    /// </para>
    /// <para>
    /// It never authorises anything destructive either way. Setup only ever runs
    /// the platform's <c>init-db</c> and <c>load-all --skip-missing</c>, and only
    /// against a database with nothing in it.
    /// </para>
    /// </remarks>
    public bool AutoSetup { get; set; } = true;

    /// <summary>
    /// The platform database, as a SQLAlchemy URL — for example
    /// <c>sqlite:///C:/ProgramData/Project CHAOS/homestead.db</c>. Empty means
    /// "whatever the platform's own default is", which the gateway then reports
    /// rather than guesses. Passed to the platform CLI as
    /// <c>CHAOS_DATABASE_URL</c>.
    /// </summary>
    public string? DatabaseUrl { get; set; }

    /// <summary>
    /// The design package directory (<c>data/</c>) the registry is built from.
    /// Empty means "derive it from the resolved Python runtime, and let the
    /// platform use its own default". Passed to the platform CLI as
    /// <c>CHAOS_DATA_DIR</c> only when set explicitly.
    /// </summary>
    public string? DataDirectory { get; set; }

    /// <summary>
    /// Where the setup transcript is written — every command, its full output
    /// and its exit code. Empty derives
    /// <c>&lt;LocalApplicationData&gt;/Project CHAOS/chaos-setup.log</c>. The
    /// path is reported on <c>GET /host/setup</c> so a failure is actionable
    /// without a stack trace.
    /// </summary>
    public string? SetupLogPath { get; set; }

    /// <summary>
    /// Where the gateway records what it last set up, so it can answer "is the
    /// registry still in step with <c>data/</c>". Empty derives
    /// <c>&lt;LocalApplicationData&gt;/Project CHAOS/chaos-setup-state.json</c>.
    /// Deleting it loses only that answer, which then reads as unknown.
    /// </summary>
    public string? SetupStateFile { get; set; }

    /// <summary>Overall budget for one setup run, across every command in it.</summary>
    public TimeSpan SetupTimeout { get; set; } = TimeSpan.FromMinutes(15);

    /// <summary>
    /// Budget for the read-only <c>status --json</c> probe. Short: it only opens
    /// the database and counts rows, and a hung probe must not look like a long
    /// import.
    /// </summary>
    public TimeSpan SetupProbeTimeout { get; set; } = TimeSpan.FromMinutes(2);

    /// <summary>
    /// Budget for a single setup command. <c>load-all</c> imports the whole
    /// design package, which on a slow disk is minutes rather than seconds.
    /// </summary>
    public TimeSpan SetupCommandTimeout { get; set; } = TimeSpan.FromMinutes(10);

    /// <summary>The backend base address, parsed. </summary>
    /// <returns>The parsed absolute URI.</returns>
    /// <exception cref="InvalidOperationException"><see cref="BackendUrl"/> is not an absolute URI.</exception>
    public Uri BackendUri() =>
        Uri.TryCreate(BackendUrl, UriKind.Absolute, out var uri)
            ? uri
            : throw new InvalidOperationException($"Chaos:BackendUrl is not an absolute URI: '{BackendUrl}'.");
}
