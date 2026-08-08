namespace Chaos.Shell.Core;

/// <summary>
/// How one check is drawn.
/// </summary>
/// <remarks>
/// <see cref="Unknown"/> is the default on purpose. A check that has not been
/// made is not a check that passed, and every state in this enum other than
/// <see cref="Pass"/> keeps that distinction visible.
/// </remarks>
public enum CheckState
{
    /// <summary>Not established. Never drawn as calm.</summary>
    Unknown = 0,

    /// <summary>Not attempted yet, and will be.</summary>
    Pending = 1,

    /// <summary>Being established right now.</summary>
    Checking = 2,

    /// <summary>Verified good.</summary>
    Pass = 3,

    /// <summary>Verified, with something an operator should know.</summary>
    Warn = 4,

    /// <summary>Verified bad.</summary>
    Fail = 5,

    /// <summary>Does not apply in this configuration, with a reason given.</summary>
    NotApplicable = 6,
}

/// <summary>The checks the launcher shows, in the order it shows them.</summary>
public enum LauncherCheckId
{
    /// <summary>Is anything answering at the configured gateway address?</summary>
    Gateway = 0,

    /// <summary>Is the Windows service installed, and what is it doing?</summary>
    WindowsService = 1,

    /// <summary>Can this shell find a gateway executable to start itself?</summary>
    HostExecutable = 2,

    /// <summary>What does <c>/health</c> say about the Python backend?</summary>
    Backend = 3,

    /// <summary>What does <c>/host/setup</c> say about first-run setup?</summary>
    Setup = 4,
}

/// <summary>One line of the launcher's findings.</summary>
/// <param name="Id">Which check.</param>
/// <param name="Label">Its name, including the thing it names (address, service name).</param>
/// <param name="State">How to draw it.</param>
/// <param name="Detail">A sentence. Never empty.</param>
public sealed record LauncherCheck(LauncherCheckId Id, string Label, CheckState State, string Detail);

/// <summary>Everything the launcher can offer to do.</summary>
public enum LauncherAction
{
    /// <summary>Start the platform, by whichever means the plan chose.</summary>
    StartPlatform = 0,

    /// <summary>Stop the platform. Only ever offered when the shell knows how.</summary>
    StopPlatform = 1,

    /// <summary>Run or retry first-run setup.</summary>
    RunSetup = 2,

    /// <summary>Show the operator console.</summary>
    OpenConsole = 3,

    /// <summary>Open the settings window.</summary>
    OpenSettings = 4,

    /// <summary>Open the log folder.</summary>
    OpenLogs = 5,

    /// <summary>Restart this shell with administrator rights.</summary>
    RelaunchElevated = 6,

    /// <summary>Look again, now.</summary>
    RecheckNow = 7,
}

/// <summary>Whether an action can be taken.</summary>
public enum ActionAvailability
{
    /// <summary>Press it.</summary>
    Available = 0,

    /// <summary>
    /// Shown, but it cannot be taken. The reason is always given: a control
    /// that is greyed out with no explanation makes an operator's next move a
    /// guess.
    /// </summary>
    Unavailable = 1,

    /// <summary>Available, and expected to need administrator rights.</summary>
    NeedsElevation = 2,

    /// <summary>Already running — pressing again would do nothing.</summary>
    InProgress = 3,
}

/// <summary>One button.</summary>
/// <param name="Action">Which action.</param>
/// <param name="Label">What the button says.</param>
/// <param name="Availability">Whether it can be pressed.</param>
/// <param name="Reason">
/// Why it cannot be pressed, or what pressing it will do. Never empty for
/// anything other than a plainly available action.
/// </param>
/// <param name="IsPrimary">The one thing this screen is asking for.</param>
public sealed record LauncherActionOffer(
    LauncherAction Action,
    string Label,
    ActionAvailability Availability,
    string Reason,
    bool IsPrimary)
{
    public bool CanInvoke => Availability
        is ActionAvailability.Available or ActionAvailability.NeedsElevation;
}

/// <summary>Where the launcher is in getting the operator to a usable console.</summary>
public enum LauncherPhase
{
    /// <summary>First pass. Nothing established.</summary>
    Checking = 0,

    /// <summary>Nothing answers, and there is something the operator can do about it.</summary>
    PlatformDown = 1,

    /// <summary>A start is under way — service starting, or a child just launched.</summary>
    Starting = 2,

    /// <summary>The gateway answers but the Python backend does not.</summary>
    BackendDown = 3,

    /// <summary>The platform is up but has not been set up.</summary>
    SetupRequired = 4,

    /// <summary>Setup is running.</summary>
    SettingUp = 5,

    /// <summary>Everything verified. The console can be shown.</summary>
    Ready = 6,

    /// <summary>
    /// Nothing answers and this shell has no way to fix it — no service, no
    /// executable, or an address that is not this machine.
    /// </summary>
    Blocked = 7,
}

/// <summary>
/// Everything the launcher window draws, decided in one place.
/// </summary>
/// <param name="Phase">Where things stand.</param>
/// <param name="Headline">The big line. Calm, specific, never a diagnostic dump.</param>
/// <param name="Summary">One paragraph saying what was found and what happens next.</param>
/// <param name="Checks">The findings, in a fixed order.</param>
/// <param name="Actions">Buttons, primary first.</param>
/// <param name="RunMode">Who owns the platform, for the permanent banner.</param>
/// <param name="Setup">The setup panel, whether or not it is shown.</param>
/// <param name="ShowProgress">Whether something is happening that will change on its own.</param>
/// <param name="ConsoleIsUsable">
/// Whether the console can be shown without lying to the operator. This gates
/// the WebView2: it is never navigated before this is true, because a blank
/// white window says nothing about whether a homestead is being controlled.
/// </param>
/// <param name="Notices">
/// Things the operator has to be told that are not checks — a rejected setting,
/// the result of their last action, a stale-configuration warning.
/// </param>
public sealed record LauncherView(
    LauncherPhase Phase,
    string Headline,
    string Summary,
    IReadOnlyList<LauncherCheck> Checks,
    IReadOnlyList<LauncherActionOffer> Actions,
    RunModeBanner RunMode,
    SetupView Setup,
    bool ShowProgress,
    bool ConsoleIsUsable,
    IReadOnlyList<string> Notices)
{
    /// <summary>The button this screen is asking for, if any.</summary>
    public LauncherActionOffer? Primary => Actions.FirstOrDefault(a => a.IsPrimary);

    /// <summary>
    /// Whether the launcher may replace itself with the console without anyone
    /// asking it to.
    /// </summary>
    /// <remarks>
    /// Set by <see cref="LauncherStateMachine"/> only when everything that
    /// could be verified was, or when the one thing that could not be verified
    /// is a known, benign gap — a gateway too old to report setup state. An
    /// anomaly the shell cannot explain keeps the operator on this screen,
    /// where the anomaly is written down, instead of hiding it behind a console
    /// that looks fine.
    /// </remarks>
    public bool SafeToAdvanceUnattended { get; init; }

    /// <summary>Finds an offer, for the window's click handlers.</summary>
    public LauncherActionOffer? Offer(LauncherAction action) =>
        Actions.FirstOrDefault(a => a.Action == action);

    /// <summary>Finds a check, for tests and for the window's rows.</summary>
    public LauncherCheck? Check(LauncherCheckId id) => Checks.FirstOrDefault(c => c.Id == id);
}
