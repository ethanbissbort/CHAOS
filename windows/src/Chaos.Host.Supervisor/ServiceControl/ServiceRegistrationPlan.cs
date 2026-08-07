using System.Globalization;

namespace Chaos.Host.Supervisor.ServiceControl;

/// <summary>How the Windows Service should be registered.</summary>
public sealed class ServiceInstallOptions
{
    /// <summary>Service key name. Also the virtual account name (<c>NT SERVICE\&lt;name&gt;</c>).</summary>
    public string ServiceName { get; set; } = "ChaosHost";

    public string DisplayName { get; set; } = "Project CHAOS Host";

    public string Description { get; set; } =
        "Central Homestead Automation and Operation System. Hosts the operator gateway and supervises " +
        "the platform backend that controls energy, water and alarms.";

    /// <summary>
    /// Full path to the gateway executable. Defaults to the current process's
    /// executable, which is right when an admin runs <c>--install-service</c>
    /// and wrong when an installer builds the plan for a not-yet-copied file —
    /// so the installer sets it explicitly.
    /// </summary>
    public string? BinaryPath { get; set; }

    /// <summary>Argument appended to the registered binary path.</summary>
    public string ServiceArgument { get; set; } = "--service";

    /// <summary>
    /// <c>auto</c>, <c>delayed-auto</c> or <c>demand</c>. Auto by default:
    /// this is the control plane for the property's power and water, and it
    /// should be up before anyone logs in.
    /// </summary>
    public string StartMode { get; set; } = "auto";

    /// <summary>
    /// Service account. A virtual account (<c>NT SERVICE\&lt;ServiceName&gt;</c>)
    /// is created automatically by SCM, needs no password and no secret in the
    /// installer, and is a far smaller blast radius than LocalSystem.
    /// </summary>
    public string Account { get; set; } = @"NT SERVICE\ChaosHost";

    /// <summary>Empty for a virtual account.</summary>
    public string Password { get; set; } = string.Empty;

    /// <summary>Services that must start first.</summary>
    public IList<string> Dependencies { get; } = new List<string> { "Tcpip" };

    // -- Recovery ----------------------------------------------------------

    /// <summary>Window after which the failure count resets. One day.</summary>
    public TimeSpan RecoveryResetPeriod { get; set; } = TimeSpan.FromDays(1);

    /// <summary>Delay before SCM's first restart.</summary>
    public TimeSpan FirstRestartDelay { get; set; } = TimeSpan.FromSeconds(5);

    /// <summary>Delay before SCM's second restart.</summary>
    public TimeSpan SecondRestartDelay { get; set; } = TimeSpan.FromSeconds(15);

    /// <summary>Delay before every subsequent restart.</summary>
    public TimeSpan SubsequentRestartDelay { get; set; } = TimeSpan.FromMinutes(1);

    /// <summary>
    /// Apply recovery actions when the service stops with a non-zero exit code,
    /// not only when it crashes. On by default: the supervisor exits non-zero
    /// when its circuit breaker trips, and that is exactly the case where an
    /// unattended machine should get one more supervised chance.
    /// </summary>
    public bool RecoverOnNonCrashFailure { get; set; } = true;

    // -- Event Log ---------------------------------------------------------

    public string EventLogSource { get; set; } = "Project CHAOS";

    public string EventLogName { get; set; } = "Application";
}

/// <summary>One command in an installation plan.</summary>
public sealed record ServiceCommand(string FileName, IReadOnlyList<string> Arguments, string Purpose)
{
    /// <summary>The command as an administrator would type it.</summary>
    public string ToCommandLine() =>
        string.Join(' ', new[] { FileName }.Concat(Arguments).Select(Quote));

    private static string Quote(string value) =>
        value.Length == 0 || value.Contains(' ', StringComparison.Ordinal)
            ? "\"" + value + "\""
            : value;
}

/// <summary>
/// The exact <c>sc.exe</c> commands that register and remove the service.
/// </summary>
/// <remarks>
/// <para>
/// <b>Who owns registration.</b> The commands are produced here and executed by
/// <c>sc.exe</c> — either by the packaging installer (the normal path) or by an
/// administrator running <c>--install-service</c> at an elevated prompt (the
/// hand path). The split is deliberate:
/// </para>
/// <list type="bullet">
/// <item>Creating a service and setting recovery needs administrator rights.
/// A supervisor library must not be in the business of asking for elevation.</item>
/// <item>The installer already runs elevated, already knows the final install
/// directory, and already owns rollback on uninstall. Registration belongs to
/// whoever owns rollback.</item>
/// <item>But <em>what</em> to register — the binary path, the recovery timings,
/// the account, the Event Log source — is knowledge that belongs with this
/// code. Duplicating it into an installer script is how the two drift.</item>
/// </list>
/// <para>
/// So: this class is the single source of truth, and both paths execute the
/// same plan. The packaging agent should call <see cref="Install"/> and run the
/// commands, rather than writing its own.
/// </para>
/// </remarks>
public static class ServiceRegistrationPlan
{
    /// <summary>Commands that register the service, in order.</summary>
    public static IReadOnlyList<ServiceCommand> Install(ServiceInstallOptions options)
    {
        ArgumentNullException.ThrowIfNull(options);

        var binary = options.BinaryPath is { Length: > 0 } path
            ? path
            : Environment.ProcessPath ?? throw new InvalidOperationException(
                "Could not determine the executable path. Set ServiceInstallOptions.BinaryPath.");

        var binaryPathValue = options.ServiceArgument is { Length: > 0 } argument
            ? $"\"{binary}\" {argument}"
            : $"\"{binary}\"";

        var commands = new List<ServiceCommand>
        {
            new(
                "sc.exe",
                [
                    "create",
                    options.ServiceName,
                    "binPath=",
                    binaryPathValue,
                    "DisplayName=",
                    options.DisplayName,
                    "start=",
                    options.StartMode,
                    "obj=",
                    options.Account,
                    "password=",
                    options.Password,
                    "depend=",
                    string.Join('/', options.Dependencies),
                ],
                "create the service under a virtual account with no stored password"),

            new(
                "sc.exe",
                ["description", options.ServiceName, options.Description],
                "describe the service in services.msc"),

            new(
                "sc.exe",
                [
                    "failure",
                    options.ServiceName,
                    "reset=",
                    Seconds(options.RecoveryResetPeriod),
                    "actions=",
                    $"restart/{Milliseconds(options.FirstRestartDelay)}/" +
                    $"restart/{Milliseconds(options.SecondRestartDelay)}/" +
                    $"restart/{Milliseconds(options.SubsequentRestartDelay)}",
                ],
                "SCM restarts the whole host if it dies: after 5s, then 15s, then every 60s, " +
                "with the failure count reset once a day"),
        };

        if (options.RecoverOnNonCrashFailure)
        {
            commands.Add(new ServiceCommand(
                "sc.exe",
                ["failureflag", options.ServiceName, "1"],
                "treat a non-zero exit as a failure too, not just a crash — the supervisor exits " +
                "non-zero when its circuit breaker trips"));
        }

        commands.Add(new ServiceCommand(
            "powershell.exe",
            [
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                $"if (-not [System.Diagnostics.EventLog]::SourceExists('{options.EventLogSource}')) " +
                $"{{ New-EventLog -LogName {options.EventLogName} -Source '{options.EventLogSource}' }}",
            ],
            "register the Event Log source once, while elevated, so the service never needs to"));

        return commands;
    }

    /// <summary>Commands that remove the service, in order.</summary>
    public static IReadOnlyList<ServiceCommand> Uninstall(ServiceInstallOptions options)
    {
        ArgumentNullException.ThrowIfNull(options);

        return
        [
            new ServiceCommand(
                "sc.exe",
                ["stop", options.ServiceName],
                "stop the service; failure here is expected when it is already stopped"),
            new ServiceCommand(
                "sc.exe",
                ["delete", options.ServiceName],
                "remove the service registration"),
            new ServiceCommand(
                "powershell.exe",
                [
                    "-NoProfile",
                    "-NonInteractive",
                    "-Command",
                    $"if ([System.Diagnostics.EventLog]::SourceExists('{options.EventLogSource}')) " +
                    $"{{ Remove-EventLog -Source '{options.EventLogSource}' }}",
                ],
                "remove the Event Log source"),
        ];
    }

    /// <summary>The plan as a script an operator can read, check, and run by hand.</summary>
    public static string Describe(IReadOnlyList<ServiceCommand> plan)
    {
        ArgumentNullException.ThrowIfNull(plan);

        return string.Join(
            Environment.NewLine,
            plan.Select(command => $":: {command.Purpose}{Environment.NewLine}{command.ToCommandLine()}"));
    }

    private static string Seconds(TimeSpan value) =>
        ((long)value.TotalSeconds).ToString(CultureInfo.InvariantCulture);

    private static string Milliseconds(TimeSpan value) =>
        ((long)value.TotalMilliseconds).ToString(CultureInfo.InvariantCulture);
}
