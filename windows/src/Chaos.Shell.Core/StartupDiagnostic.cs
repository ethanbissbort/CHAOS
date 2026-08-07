namespace Chaos.Shell.Core;

/// <summary>A label/value row in the diagnostic panel.</summary>
public sealed record DiagnosticFact(string Label, string Value);

/// <summary>
/// The content shown when the shell cannot reach the gateway, or when WebView2
/// is missing.
/// </summary>
/// <remarks>
/// This exists so the failure path is built from tested data rather than
/// assembled ad hoc in XAML. The worst outcome at startup is a blank white
/// WebView2: it looks like a hung application, tells an operator nothing, and
/// invites them to conclude the platform is down when it may be running
/// perfectly well behind a shell that simply cannot see it.
/// </remarks>
public sealed record StartupDiagnostic
{
    public required string Title { get; init; }

    /// <summary>One sentence stating plainly what is and is not known.</summary>
    public required string Summary { get; init; }

    public required IReadOnlyList<DiagnosticFact> Facts { get; init; }

    public required IReadOnlyList<string> NextSteps { get; init; }

    /// <summary>
    /// Whether the platform itself is implicated. False means "the shell cannot
    /// see it", which is not the same claim and must not be presented as one.
    /// </summary>
    public bool PlatformStateKnown { get; init; }

    /// <summary>
    /// The gateway never answered within the startup budget.
    /// </summary>
    public static StartupDiagnostic GatewayUnreachable(
        HostEndpoints endpoints,
        int attempts,
        TimeSpan elapsed,
        string? lastError,
        string logDirectory,
        string serviceName)
    {
        ArgumentNullException.ThrowIfNull(endpoints);

        return new StartupDiagnostic
        {
            Title = "Cannot reach the CHAOS gateway",

            // Deliberate wording. The shell knows one thing — that it got no
            // answer. It does not know whether the platform is down, and says
            // so rather than implying either.
            Summary =
                $"The desktop shell got no answer from {endpoints.BaseUri} after "
                + $"{RelativeTime.Describe(elapsed)}. This means the shell cannot see the platform. "
                + "It does not by itself mean the platform has stopped: control, alarm evaluation "
                + "and logging run in the Windows service, independently of this window.",

            Facts = new[]
            {
                new DiagnosticFact("Gateway address", endpoints.BaseUri.ToString()),
                new DiagnosticFact("Readiness probe", endpoints.Health.ToString()),
                new DiagnosticFact("Attempts", attempts.ToString(System.Globalization.CultureInfo.InvariantCulture)),
                new DiagnosticFact("Waited", RelativeTime.Describe(elapsed)),
                new DiagnosticFact("Last error", string.IsNullOrWhiteSpace(lastError) ? "(none reported)" : lastError!),
                new DiagnosticFact("Windows service", serviceName),
                new DiagnosticFact("Shell log", logDirectory),
            },

            NextSteps = new[]
            {
                $"Check the service is running: sc query \"{serviceName}\"",
                $"Try the address in a browser: {endpoints.Health}",
                $"Read the shell log in {logDirectory}",
                "If the gateway moved, start the shell with --host <address> "
                + $"or set {HostUrlResolver.EnvironmentVariable}.",
            },

            PlatformStateKnown = false,
        };
    }

    /// <summary>
    /// The WebView2 runtime is not installed. The shell stays up and says what
    /// to install; it must not crash and must not show an empty frame.
    /// </summary>
    public static StartupDiagnostic WebViewRuntimeMissing(HostEndpoints endpoints, string? detail)
    {
        ArgumentNullException.ThrowIfNull(endpoints);

        return new StartupDiagnostic
        {
            Title = "WebView2 runtime is not installed",

            Summary =
                "This shell renders the operator console with Microsoft Edge WebView2, which is "
                + "not present on this machine. The platform is unaffected — the same console is "
                + "still served to any browser on the LAN.",

            Facts = new[]
            {
                new DiagnosticFact("Console address", endpoints.Console.ToString()),
                new DiagnosticFact("Annunciator address", endpoints.Annunciator.ToString()),
                new DiagnosticFact("Required component", "Microsoft Edge WebView2 Evergreen Runtime"),
                new DiagnosticFact("Detail", string.IsNullOrWhiteSpace(detail) ? "(none reported)" : detail!),
            },

            NextSteps = new[]
            {
                "Install the WebView2 Evergreen Runtime, then restart this shell.",
                "The installer is redistributable and works offline: keep a copy on the node.",
                $"In the meantime, open {endpoints.Console} in any browser.",
            },

            PlatformStateKnown = false,
        };
    }
}
