using System.Collections.ObjectModel;
using System.Globalization;
using System.Net;

namespace Chaos.Host.Supervisor;

/// <summary>
/// Everything the supervisor needs to launch, watch and restart the Python
/// platform. Bind from configuration section <c>Chaos:Backend</c>.
/// </summary>
public sealed class BackendSupervisorOptions
{
    /// <summary>Configuration section this binds from by convention.</summary>
    public const string SectionName = "Chaos:Backend";

    // -- Layout ------------------------------------------------------------

    /// <summary>
    /// Install root. The embedded runtime is looked for at
    /// <c>&lt;InstallRoot&gt;\python\python.exe</c>. Defaults to the directory
    /// the host assembly was loaded from.
    /// </summary>
    public string? InstallRoot { get; set; }

    /// <summary>
    /// Repository root for the DEVELOPMENT layout — the directory holding
    /// <c>src/chaos/cli.py</c>. When null the resolver walks up from
    /// <see cref="InstallRoot"/> looking for it.
    /// </summary>
    public string? RepositoryRoot { get; set; }

    /// <summary>
    /// Explicit interpreter for the DEVELOPMENT layout. When null the resolver
    /// probes PATH for <c>python3</c> then <c>python</c>.
    /// </summary>
    public string? PythonExecutable { get; set; }

    /// <summary>
    /// Refuse the development layout entirely. Set this in the packaged
    /// product: a shipped install that has lost its embedded runtime must fail
    /// loudly, not quietly borrow whatever Python the machine happens to have.
    /// </summary>
    public bool RequireEmbeddedRuntime { get; set; }

    // -- Binding -----------------------------------------------------------

    /// <summary>Address the backend binds. Loopback only — the gateway is the front door.</summary>
    public string BindAddress { get; set; } = "127.0.0.1";

    /// <summary>
    /// Backend port. Defaults to the platform's own documented default so a
    /// gateway configured from <c>docs/deployment.md</c> lines up without
    /// extra configuration.
    /// </summary>
    public int Port { get; set; } = 8000;

    /// <summary>
    /// Escape hatch for a non-loopback bind. Off, and it should stay off:
    /// exposing the platform directly bypasses the gateway's authentication.
    /// </summary>
    public bool AllowNonLoopbackBind { get; set; }

    /// <summary>Readiness path on the backend.</summary>
    public string HealthPath { get; set; } = "/health";

    // -- Child process -----------------------------------------------------

    /// <summary>Value for the CLI's global <c>--log-level</c> flag.</summary>
    public string LogLevel { get; set; } = "INFO";

    /// <summary>Sets <c>CHAOS_DATABASE_URL</c> when non-null.</summary>
    public string? DatabaseUrl { get; set; }

    /// <summary>Sets <c>CHAOS_DATA_DIR</c> when non-null.</summary>
    public string? DataDirectory { get; set; }

    /// <summary>
    /// Extra environment for the child, applied last so it wins over the
    /// supervisor's own defaults.
    /// </summary>
    public IDictionary<string, string?> Environment { get; } =
        new Dictionary<string, string?>(StringComparer.Ordinal);

    /// <summary>Extra arguments appended after <c>serve --host … --port …</c>.</summary>
    public IList<string> ExtraServeArguments { get; } = new List<string>();

    /// <summary>Lines of child stderr kept for the operator snapshot.</summary>
    public int StandardErrorTailLines { get; set; } = 60;

    // -- Readiness and health ---------------------------------------------

    /// <summary>
    /// How long a freshly launched backend has to answer <c>/health</c> before
    /// the attempt is treated as failed. The platform imports SQLAlchemy,
    /// FastAPI and every router, and creates tables on first run.
    /// </summary>
    public TimeSpan ReadinessTimeout { get; set; } = TimeSpan.FromSeconds(90);

    /// <summary>Interval between <c>/health</c> polls, during startup and after.</summary>
    public TimeSpan HealthPollInterval { get; set; } = TimeSpan.FromSeconds(2);

    /// <summary>Per-probe HTTP timeout.</summary>
    public TimeSpan HealthCheckTimeout { get; set; } = TimeSpan.FromSeconds(5);

    /// <summary>
    /// Consecutive failed probes on a live process before it is declared dead
    /// and recycled. More than one so a single GC pause or a slow query does
    /// not bounce a control system.
    /// </summary>
    public int UnhealthyProbeThreshold { get; set; } = 3;

    // -- Restart policy ----------------------------------------------------

    /// <summary>Delay before the first restart.</summary>
    public TimeSpan InitialRestartDelay { get; set; } = TimeSpan.FromSeconds(2);

    /// <summary>Ceiling for the exponential backoff.</summary>
    public TimeSpan MaxRestartDelay { get; set; } = TimeSpan.FromMinutes(2);

    /// <summary>Backoff growth factor.</summary>
    public double BackoffMultiplier { get; set; } = 2.0;

    /// <summary>
    /// Random fraction added to each backoff delay (0.0–1.0). Stops the
    /// supervisor, the gateway's health poller and the shell's retry loop from
    /// marching in lockstep after a shared outage.
    /// </summary>
    public double BackoffJitterFraction { get; set; } = 0.2;

    /// <summary>
    /// Failures inside <see cref="FailureWindow"/> that trip the circuit
    /// breaker into terminal <c>Failed</c>.
    /// </summary>
    public int MaxStartFailures { get; set; } = 5;

    /// <summary>Sliding window the breaker counts failures in.</summary>
    public TimeSpan FailureWindow { get; set; } = TimeSpan.FromMinutes(10);

    /// <summary>
    /// How long a backend must stay healthy before the failure history is
    /// forgiven. Without this, five crashes spread over a month would
    /// eventually trip the breaker on a system that is basically fine.
    /// </summary>
    public TimeSpan HealthyRunDuration { get; set; } = TimeSpan.FromMinutes(2);

    // -- Shutdown ----------------------------------------------------------

    /// <summary>
    /// Time the child gets to exit after the graceful signal before the process
    /// tree is killed.
    /// </summary>
    public TimeSpan GracefulShutdownTimeout { get; set; } = TimeSpan.FromSeconds(20);

    // -- Host integration --------------------------------------------------

    /// <summary>
    /// Block <see cref="BackendSupervisor.StartAsync"/> until the backend is
    /// <c>Running</c> or terminally <c>Failed</c>, up to
    /// <see cref="StartupGate"/>. Even when true it never blocks forever — the
    /// gateway must come up and answer 503 rather than hang.
    /// </summary>
    public bool WaitForReadyOnStart { get; set; } = true;

    /// <summary>Upper bound on the <see cref="WaitForReadyOnStart"/> wait.</summary>
    public TimeSpan StartupGate { get; set; } = TimeSpan.FromSeconds(60);

    /// <summary>Register the supervisor as an <c>IHostedService</c>.</summary>
    public bool RegisterHostedService { get; set; } = true;

    // -- Windows Event Log -------------------------------------------------

    /// <summary>Event Log source. Created at install time; see README.md.</summary>
    public string EventLogSource { get; set; } = "Project CHAOS";

    /// <summary>Event Log to write to.</summary>
    public string EventLogName { get; set; } = "Application";

    /// <summary>
    /// Try to create the Event Log source at runtime if it is missing. Needs
    /// administrator rights, so it is off by default: a service running as a
    /// virtual account will simply be told it cannot, and log that fact.
    /// </summary>
    public bool CreateEventLogSourceIfMissing { get; set; }

    // -- Derived -----------------------------------------------------------

    /// <summary>The <c>/health</c> URL the readiness probe polls.</summary>
    public Uri HealthEndpoint =>
        new($"http://{FormatHostForUri(BindAddress)}:{Port.ToString(CultureInfo.InvariantCulture)}{NormalisedHealthPath}");

    /// <summary>Backend root, for logging and for the gateway's proxy destination.</summary>
    public Uri BaseAddress =>
        new($"http://{FormatHostForUri(BindAddress)}:{Port.ToString(CultureInfo.InvariantCulture)}/");

    private string NormalisedHealthPath =>
        HealthPath.StartsWith('/') ? HealthPath : "/" + HealthPath;

    private static string FormatHostForUri(string host) =>
        IPAddress.TryParse(host, out var parsed) && parsed.AddressFamily == System.Net.Sockets.AddressFamily.InterNetworkV6
            ? "[" + parsed.ToString() + "]"
            : host;

    /// <summary>
    /// Every problem with this configuration, in one pass. Returns an empty
    /// list when the options are usable.
    /// </summary>
    public IReadOnlyList<string> Validate()
    {
        var errors = new List<string>();

        if (string.IsNullOrWhiteSpace(BindAddress))
        {
            errors.Add($"{nameof(BindAddress)} must be set.");
        }
        else if (!AllowNonLoopbackBind && !IsLoopback(BindAddress))
        {
            errors.Add(
                $"{nameof(BindAddress)} '{BindAddress}' is not a loopback address. The platform is " +
                $"supervised behind the gateway and must not be reachable directly. Set " +
                $"{nameof(AllowNonLoopbackBind)}=true only if you have decided otherwise on purpose.");
        }

        if (Port is < 1 or > 65535)
        {
            errors.Add($"{nameof(Port)} must be 1–65535 (got {Port.ToString(CultureInfo.InvariantCulture)}). " +
                       "Port 0 cannot be used: the supervisor would not know where to poll /health.");
        }

        if (string.IsNullOrWhiteSpace(HealthPath))
        {
            errors.Add($"{nameof(HealthPath)} must be set.");
        }

        RequirePositive(errors, ReadinessTimeout, nameof(ReadinessTimeout));
        RequirePositive(errors, HealthPollInterval, nameof(HealthPollInterval));
        RequirePositive(errors, HealthCheckTimeout, nameof(HealthCheckTimeout));
        RequirePositive(errors, InitialRestartDelay, nameof(InitialRestartDelay));
        RequirePositive(errors, MaxRestartDelay, nameof(MaxRestartDelay));
        RequirePositive(errors, FailureWindow, nameof(FailureWindow));
        RequirePositive(errors, GracefulShutdownTimeout, nameof(GracefulShutdownTimeout));

        if (MaxRestartDelay < InitialRestartDelay)
        {
            errors.Add($"{nameof(MaxRestartDelay)} must be at least {nameof(InitialRestartDelay)}.");
        }

        if (BackoffMultiplier < 1.0)
        {
            errors.Add($"{nameof(BackoffMultiplier)} must be at least 1.0, otherwise backoff shrinks.");
        }

        if (BackoffJitterFraction is < 0.0 or > 1.0)
        {
            errors.Add($"{nameof(BackoffJitterFraction)} must be between 0.0 and 1.0.");
        }

        if (MaxStartFailures < 1)
        {
            errors.Add($"{nameof(MaxStartFailures)} must be at least 1, otherwise the circuit breaker " +
                       "can never trip and a crash loop looks like 'starting' forever.");
        }

        if (UnhealthyProbeThreshold < 1)
        {
            errors.Add($"{nameof(UnhealthyProbeThreshold)} must be at least 1.");
        }

        if (StandardErrorTailLines < 1)
        {
            errors.Add($"{nameof(StandardErrorTailLines)} must be at least 1.");
        }

        if (string.IsNullOrWhiteSpace(EventLogSource))
        {
            errors.Add($"{nameof(EventLogSource)} must be set.");
        }

        return new ReadOnlyCollection<string>(errors);
    }

    /// <summary>Throws when <see cref="Validate"/> found anything.</summary>
    public void ThrowIfInvalid()
    {
        var errors = Validate();
        if (errors.Count == 0)
        {
            return;
        }

        throw new BackendSupervisorConfigurationException(
            "The backend supervisor is misconfigured:" + System.Environment.NewLine +
            string.Join(System.Environment.NewLine, errors.Select(e => "  - " + e)));
    }

    private static void RequirePositive(ICollection<string> errors, TimeSpan value, string name)
    {
        if (value <= TimeSpan.Zero)
        {
            errors.Add($"{name} must be greater than zero.");
        }
    }

    internal static bool IsLoopback(string host)
    {
        if (string.Equals(host, "localhost", StringComparison.OrdinalIgnoreCase))
        {
            return true;
        }

        return IPAddress.TryParse(host, out var address) && IPAddress.IsLoopback(address);
    }
}

/// <summary>Thrown when the supervisor cannot be configured into a usable state.</summary>
public sealed class BackendSupervisorConfigurationException : Exception
{
    public BackendSupervisorConfigurationException()
    {
    }

    public BackendSupervisorConfigurationException(string message)
        : base(message)
    {
    }

    public BackendSupervisorConfigurationException(string message, Exception innerException)
        : base(message, innerException)
    {
    }
}
