namespace Chaos.Shell.Core;

/// <summary>
/// Operator-facing wording that carries a safety meaning, kept in one tested
/// place rather than scattered through XAML.
/// </summary>
/// <remarks>
/// These strings exist because of a specific failure mode: an operator closes
/// the desktop window, assumes the platform stopped with it, and stops treating
/// the site as live. The platform runs as a Windows service and keeps
/// controlling, evaluating alarms and logging whether or not this shell is
/// open. Every place the shell disappears from view has to say so.
/// </remarks>
public static class ShellMessages
{
    /// <summary>Default name of the supervising Windows service.</summary>
    /// <remarks>
    /// This MUST match <c>ChaosServiceName</c> in
    /// <c>windows/installer/Chaos.Definitions.wxi</c>, which is what actually
    /// registers the service. It previously read "ChaosPlatform" while the
    /// installer registered "ChaosHost", so <see cref="System.ServiceProcess"/>
    /// found nothing and the launcher reported an installed, running service as
    /// not installed -- then offered to start the platform a second time.
    /// </remarks>
    public const string ServiceName = "ChaosHost";

    /// <summary>Balloon shown the first time the main window is closed to tray.</summary>
    public const string HiddenToTrayTitle = "Project CHAOS is still running";

    public const string HiddenToTrayBody =
        "The window closed; the platform did not. Control, alarm evaluation and logging "
        + "continue in the Windows service. Use the tray icon to reopen the console, "
        + "or Exit to close this shell (which still does not stop the platform).";

    /// <summary>Confirmation shown when Exit is chosen from the tray.</summary>
    public const string ExitConfirmationTitle = "Close the desktop shell?";

    public const string ExitConfirmationBody =
        "This closes the desktop window and the tray icon only. The Project CHAOS "
        + "service keeps running: the site stays controlled and alarms keep being "
        + "evaluated, but this machine will stop showing them. To stop the platform "
        + "itself, stop the Windows service deliberately.";

    /// <summary>Permanent footer on the main window.</summary>
    public const string ServiceRunsIndependently =
        "The platform runs as a Windows service, independently of this window.";

    /// <summary>
    /// What the tray "Service status" item shows when the shell has no current
    /// answer. Never a reassuring word — the shell does not know.
    /// </summary>
    public const string ServiceStatusUnknown =
        "Service status unknown — the shell cannot reach the gateway. "
        + "Check the service directly before assuming either way.";

    /// <summary>Why Acknowledge opens the panel rather than acting directly.</summary>
    public const string AcknowledgeOpensPanel =
        "Acknowledgement is an audited action taken by a named operator on the "
        + "annunciator panel. The shell opens the panel; it never acknowledges for you.";

    /// <summary>
    /// Title of the exit prompt when this shell is the platform's parent
    /// process. Deliberately a question about the platform, not about a window.
    /// </summary>
    public const string ExitStopsPlatformTitle = "Exit and STOP the platform?";

    /// <summary>
    /// The confirmation for exiting, worded for how the platform is actually
    /// running.
    /// </summary>
    /// <remarks>
    /// The one case this exists for: a platform started by this shell dies with
    /// it. An operator who has learnt from every other day that closing the
    /// window is harmless must be stopped and told, in a different sentence,
    /// that today it is not.
    /// </remarks>
    public static string ExitConfirmationFor(PlatformRunMode mode, string serviceName = ServiceName) =>
        mode switch
        {
            PlatformRunMode.ManagedByThisShell =>
                "This shell started the platform and is its parent process. Exiting STOPS IT: "
                + "control, alarm evaluation, logging and freeze protection all stop on this node "
                + "until it is started again. Nothing else on this machine will bring it back. "
                + $"To keep the platform running without this window, install it as the Windows "
                + $"service '{serviceName}'.",

            PlatformRunMode.WindowsService =>
                $"This closes the desktop window and the tray icon only. The Windows service "
                + $"'{serviceName}' keeps running: the site stays controlled and alarms keep being "
                + "evaluated, but this machine will stop showing them. To stop the platform itself, "
                + "stop the Windows service deliberately.",

            PlatformRunMode.Foreign =>
                "This closes the desktop window and the tray icon only. This shell did not start "
                + "the platform that is answering, so exiting does not stop it — but equally, this "
                + "shell cannot promise it will keep running. Whatever started it decides that.",

            _ =>
                "This closes the desktop window and the tray icon only. The shell cannot currently "
                + "see the platform, so it cannot tell you what exiting will do to it. Check the "
                + "service or the node directly before assuming either way.",
        };

    /// <summary>
    /// The tray's Exit item, worded for the run mode so the menu itself carries
    /// the warning before anything is clicked.
    /// </summary>
    public static string ExitMenuItemFor(PlatformRunMode mode) => mode switch
    {
        PlatformRunMode.ManagedByThisShell => "Exit shell — THIS STOPS THE PLATFORM",
        PlatformRunMode.WindowsService => "Exit shell (platform keeps running)",
        PlatformRunMode.Foreign => "Exit shell (does not stop the platform)",
        _ => "Exit shell",
    };
}
