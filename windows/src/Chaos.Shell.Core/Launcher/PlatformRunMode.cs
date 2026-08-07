namespace Chaos.Shell.Core;

/// <summary>
/// Who owns the running platform, from this shell's point of view.
/// </summary>
/// <remarks>
/// This is the single most consequential fact the launcher displays. An
/// operator who believes the platform is a Windows service, closes the window,
/// and walks away — when in fact this shell was the parent process — has just
/// switched off freeze protection on their homestead without knowing it. Every
/// value here exists to make that distinction impossible to miss.
/// </remarks>
public enum PlatformRunMode
{
    /// <summary>
    /// Not established. The shell has not looked, or looked and could not tell.
    /// Never rendered as "stopped": not seeing something is not evidence.
    /// </summary>
    Unknown = 0,

    /// <summary>
    /// Positively established as not running: no gateway answers, and the
    /// Windows service is installed and stopped.
    /// </summary>
    NotRunning = 1,

    /// <summary>The Windows service is running it. It outlives this shell.</summary>
    WindowsService = 2,

    /// <summary>
    /// This shell started <c>Chaos.Host.exe</c> and is its parent. It dies with
    /// this shell.
    /// </summary>
    ManagedByThisShell = 3,

    /// <summary>
    /// A gateway answers, but this shell did not start it and no service
    /// accounts for it — a console window someone left open, another shell, a
    /// debugger. The shell can neither stop it nor promise it will survive.
    /// </summary>
    Foreign = 4,
}

/// <summary>How loudly the run-mode banner should be drawn.</summary>
public enum RunModeSeverity
{
    /// <summary>Steady state, no action implied.</summary>
    Info = 0,

    /// <summary>The operator must read this before closing the window.</summary>
    Caution = 1,
}

/// <summary>
/// The permanent banner stating who owns the platform.
/// </summary>
/// <param name="Mode">The run mode this describes.</param>
/// <param name="Headline">Short enough for a status strip. Never ambiguous.</param>
/// <param name="Detail">The full sentence, for the launcher and the exit prompt.</param>
/// <param name="Severity">Drawing weight.</param>
/// <param name="ClosingTheShellStopsThePlatform">
/// The fact the exit path keys off. True only for
/// <see cref="PlatformRunMode.ManagedByThisShell"/>.
/// </param>
public sealed record RunModeBanner(
    PlatformRunMode Mode,
    string Headline,
    string Detail,
    RunModeSeverity Severity,
    bool ClosingTheShellStopsThePlatform)
{
    /// <summary>
    /// The banner for a run mode.
    /// </summary>
    /// <param name="mode">What the shell established.</param>
    /// <param name="serviceName">Name of the Windows service, for the wording.</param>
    /// <param name="gatewayAddress">Address the shell is watching, for the wording.</param>
    public static RunModeBanner For(
        PlatformRunMode mode,
        string serviceName = ShellMessages.ServiceName,
        string? gatewayAddress = null)
    {
        var address = string.IsNullOrWhiteSpace(gatewayAddress)
            ? "the configured address"
            : gatewayAddress!;

        return mode switch
        {
            PlatformRunMode.ManagedByThisShell => new RunModeBanner(
                mode,
                "Running under this shell — closing this window STOPS the platform",
                "This window started the platform and is its parent process. If you exit the shell, "
                + "sign out, or this machine restarts, control, alarm evaluation and freeze protection "
                + "STOP with it. To keep the platform running without anyone signed in, install it as "
                + $"the Windows service '{serviceName}'.",
                RunModeSeverity.Caution,
                ClosingTheShellStopsThePlatform: true),

            PlatformRunMode.WindowsService => new RunModeBanner(
                mode,
                $"Windows service '{serviceName}' — keeps running when you close this window",
                $"The platform runs as the Windows service '{serviceName}'. Closing this window closes "
                + "the view only: control, alarm evaluation and logging continue, signed in or not. "
                + "Stopping the platform is a separate, deliberate action.",
                RunModeSeverity.Info,
                ClosingTheShellStopsThePlatform: false),

            PlatformRunMode.Foreign => new RunModeBanner(
                mode,
                "Started outside this shell — this window neither runs nor stops it",
                $"A gateway is answering at {address}, but this shell did not start it and no Windows "
                + "service accounts for it. Closing this window will not stop it. Equally, this shell "
                + "cannot promise it will keep running: whoever started it decides that.",
                RunModeSeverity.Info,
                ClosingTheShellStopsThePlatform: false),

            PlatformRunMode.NotRunning => new RunModeBanner(
                mode,
                "The platform is not running",
                $"Nothing is answering at {address}, and the Windows service '{serviceName}' is "
                + "installed but stopped. As far as this shell can tell, nothing is being controlled "
                + "and no alarms are being evaluated here until it is started.",
                RunModeSeverity.Caution,
                ClosingTheShellStopsThePlatform: false),

            _ => new RunModeBanner(
                PlatformRunMode.Unknown,
                "Platform state not established",
                $"This shell cannot see a gateway at {address}. That means the shell cannot see the "
                + "platform. It is not by itself evidence that the platform has stopped — check the "
                + "service or the node directly before assuming either way.",
                RunModeSeverity.Caution,
                ClosingTheShellStopsThePlatform: false),
        };
    }
}
