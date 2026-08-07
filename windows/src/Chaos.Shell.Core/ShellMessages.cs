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
    public const string ServiceName = "ChaosPlatform";

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
}
