namespace Chaos.Shell.Core;

/// <summary>How the shell would start the platform.</summary>
public enum StartMethod
{
    /// <summary>There is no way to start it from here.</summary>
    None = 0,

    /// <summary>Ask Windows to start the service. It outlives the shell.</summary>
    WindowsService = 1,

    /// <summary>Launch the executable as a child of this shell. It dies with the shell.</summary>
    ManagedChild = 2,
}

/// <summary>
/// Exactly what pressing Start would do.
/// </summary>
/// <param name="Method">Which mechanism.</param>
/// <param name="ExecutablePath">The executable, for <see cref="StartMethod.ManagedChild"/>.</param>
/// <param name="ServiceName">The service, for <see cref="StartMethod.WindowsService"/>.</param>
/// <param name="Environment">Environment to set on a managed child.</param>
/// <param name="WorkingDirectory">Working directory for a managed child.</param>
/// <param name="Elevation">Whether administrator rights are in the way.</param>
/// <param name="ButtonLabel">What the Start button should say for this plan.</param>
/// <param name="Explanation">
/// What the operator is agreeing to. For a managed child this states, in
/// capitals, that closing the shell will stop the platform.
/// </param>
/// <param name="Refusal">Why there is nothing to press, when there is not.</param>
/// <param name="ResultingRunMode">What the run mode becomes if this succeeds.</param>
public sealed record PlatformStartPlan(
    StartMethod Method,
    string? ExecutablePath,
    string? ServiceName,
    IReadOnlyDictionary<string, string> Environment,
    string? WorkingDirectory,
    ElevationDecision Elevation,
    string ButtonLabel,
    string Explanation,
    string? Refusal,
    PlatformRunMode ResultingRunMode)
{
    public bool CanStart => Method != StartMethod.None;
}

/// <summary>
/// Chooses how to start the platform, and says so in words before anything
/// happens.
/// </summary>
/// <remarks>
/// <para>
/// The Windows service is preferred whenever one is installed, because a
/// homestead's control plane should not depend on somebody being signed in. The
/// managed child exists for development checkouts, portable copies, and the
/// case where a service exists but the operator cannot get administrator rights
/// — in which case a platform that runs only while this window is open is much
/// better than no platform, provided they are told.
/// </para>
/// <para>
/// The one thing this planner will not do is offer a Start button that cannot
/// work. A gateway address on another machine gets a refusal naming the machine,
/// not a button that starts a local process the shell will then fail to find.
/// </para>
/// </remarks>
public static class PlatformStartPlanner
{
    // The gateway binds its own options from the CHAOS_ environment prefix
    // (Chaos:ListenUrl, Chaos:Backend:*). The shell sets these three and lets
    // the gateway hand the storage settings to the Python backend itself — the
    // shell does not set the backend's own variables directly, so it cannot get
    // out of step with however the gateway chooses to pass them on.

    /// <summary>Sets the gateway's listen address (<c>Chaos:ListenUrl</c>).</summary>
    public const string ListenUrlVariable = "CHAOS_ListenUrl";

    /// <summary>Sets the backend's database URL, via <c>Chaos:Backend:DatabaseUrl</c>.</summary>
    public const string DatabaseUrlVariable = "CHAOS_Backend__DatabaseUrl";

    /// <summary>Sets the backend's data directory, via <c>Chaos:Backend:DataDirectory</c>.</summary>
    public const string DataDirectoryVariable = "CHAOS_Backend__DataDirectory";

    public static PlatformStartPlan Plan(LauncherFacts facts)
    {
        ArgumentNullException.ThrowIfNull(facts);

        var settings = facts.Settings;
        var serviceName = settings.ServiceName;

        if (!facts.AddressIsThisMachine)
        {
            return Refuse(
                $"The gateway address is {settings.HostAddress}, which is not this machine. This shell "
                + "can only start a platform running locally. Either point the address at this machine "
                + $"in Settings, or start the platform on {settings.HostAddress} itself.");
        }

        var serviceAvailable = facts.Service.CanStart;
        var executableAvailable = facts.Executable.Found;

        if (serviceAvailable && (settings.PreferWindowsService || !executableAvailable))
        {
            return ServicePlan(facts, serviceName);
        }

        if (executableAvailable)
        {
            return ChildPlan(facts);
        }

        if (serviceAvailable)
        {
            return ServicePlan(facts, serviceName);
        }

        // Nothing to press. Say which of the two routes is missing and why.
        if (facts.Service.State == PlatformServiceState.QueryFailed)
        {
            return Refuse(
                $"The shell could not read the state of the Windows service '{serviceName}', and it "
                + $"could not find {HostExecutableLocator.FileName} to start the platform itself. "
                + (facts.Service.Problem is { Length: > 0 } ? facts.Service.Problem + " " : string.Empty)
                + "Set the platform's location in Settings, or install it as a Windows service.");
        }

        if (facts.Service.IsTransitioning)
        {
            return Refuse(
                $"The Windows service '{serviceName}' is mid-transition. Wait for it to settle before "
                + "starting anything.");
        }

        return Refuse(
            $"There is no Windows service called '{serviceName}' on this machine, and "
            + $"{HostExecutableLocator.FileName} was not found in any of the places this shell looks. "
            + "Set its location in Settings, or install the platform as a Windows service.");
    }

    private static PlatformStartPlan ServicePlan(LauncherFacts facts, string serviceName)
    {
        var elevation = facts.ElevationRefused
            ? Elevation.AfterAccessDenied(facts.IsElevated, $"start the service '{serviceName}'")
            : Elevation.ForServiceControl(facts.IsElevated, "start", serviceName);

        var ignored = IgnoredStorageSettings(facts.Settings);

        return new PlatformStartPlan(
            StartMethod.WindowsService,
            ExecutablePath: null,
            ServiceName: serviceName,
            Environment: EmptyEnvironment,
            WorkingDirectory: null,
            Elevation: elevation,
            ButtonLabel: "Start platform",
            Explanation:
                $"Windows will start the service '{serviceName}'. The platform then runs under Windows: "
                + "it keeps controlling, evaluating alarms and logging when you close this window, and "
                + "it comes back after a reboot without anyone signing in."
                + (ignored is null ? string.Empty : " " + ignored)
                + (elevation.Need == ElevationNeed.NotRequired ? string.Empty : " " + elevation.Explanation),
            Refusal: null,
            ResultingRunMode: PlatformRunMode.WindowsService);
    }

    private static PlatformStartPlan ChildPlan(LauncherFacts facts)
    {
        var settings = facts.Settings;
        var environment = new Dictionary<string, string>(StringComparer.Ordinal)
        {
            // The gateway is started on all interfaces on the configured port,
            // which is what its own default does; the shell then reaches it at
            // the address the operator set.
            [ListenUrlVariable] = $"http://0.0.0.0:{settings.HostPort}",
        };

        if (settings.DatabasePath is { Length: > 0 } database)
        {
            environment[DatabaseUrlVariable] = ToDatabaseUrl(database);
        }

        if (settings.DataDirectory is { Length: > 0 } dataDirectory)
        {
            environment[DataDirectoryVariable] = dataDirectory;
        }

        var executable = facts.Executable.Path!;
        var serviceNote = facts.Service.Installed
            ? $" The Windows service '{settings.ServiceName}' is installed but is not being used, "
              + "because this shell was asked to start the platform itself."
            : $" There is no Windows service on this machine, so this is the only way this shell can "
              + "start the platform.";

        return new PlatformStartPlan(
            StartMethod.ManagedChild,
            ExecutablePath: executable,
            ServiceName: null,
            Environment: environment,
            WorkingDirectory: Path.GetDirectoryName(executable),
            Elevation: Elevation.ForManagedChild(),
            ButtonLabel: "Start platform under this shell",
            Explanation:
                $"This shell will start {executable} as a child process and keep its output in the log "
                + "below. THE PLATFORM WILL STOP WHEN YOU CLOSE THIS SHELL, and it will not come back "
                + "after a reboot on its own."
                + serviceNote,
            Refusal: null,
            ResultingRunMode: PlatformRunMode.ManagedByThisShell);
    }

    /// <summary>
    /// Says when storage settings will not be applied. Only a platform this
    /// shell starts is handed them; a service reads its own configuration.
    /// </summary>
    private static string? IgnoredStorageSettings(ShellSettings settings)
    {
        var fields = new List<string>(2);
        if (settings.DatabasePath is { Length: > 0 })
        {
            fields.Add("database location");
        }

        if (settings.DataDirectory is { Length: > 0 })
        {
            fields.Add("data directory");
        }

        if (fields.Count == 0)
        {
            return null;
        }

        return $"The {string.Join(" and ", fields)} set in Settings will NOT be applied: the service "
            + "reads its own configuration, which this shell does not change.";
    }

    /// <summary>
    /// Turns a database setting into what the platform expects. A value that is
    /// already a connection URL is passed straight through, so an operator with
    /// a non-SQLite database is not forced back into a config file.
    /// </summary>
    internal static string ToDatabaseUrl(string value)
    {
        var text = value.Trim();
        if (text.Contains("://", StringComparison.Ordinal))
        {
            return text;
        }

        // The platform's own default is sqlite:///<path>; backslashes are
        // written as forward slashes because that is what a URL holds.
        return "sqlite:///" + text.Replace('\\', '/');
    }

    private static PlatformStartPlan Refuse(string reason) => new(
        StartMethod.None,
        ExecutablePath: null,
        ServiceName: null,
        Environment: EmptyEnvironment,
        WorkingDirectory: null,
        Elevation: ElevationDecision.NotNeeded,
        ButtonLabel: "Start platform",
        Explanation: reason,
        Refusal: reason,
        ResultingRunMode: PlatformRunMode.Unknown);

    private static readonly IReadOnlyDictionary<string, string> EmptyEnvironment =
        new Dictionary<string, string>(StringComparer.Ordinal);
}
