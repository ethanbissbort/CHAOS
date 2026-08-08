namespace Chaos.Shell.Core;

/// <summary>
/// The Windows service control manager's view of the platform service, in terms
/// this shell can reason about.
/// </summary>
public enum PlatformServiceState
{
    /// <summary>Not looked at yet.</summary>
    Unknown = 0,

    /// <summary>The service control manager has no such service.</summary>
    NotInstalled = 1,

    /// <summary>Installed, not running.</summary>
    Stopped = 2,

    /// <summary>Starting.</summary>
    StartPending = 3,

    /// <summary>Running.</summary>
    Running = 4,

    /// <summary>Stopping.</summary>
    StopPending = 5,

    /// <summary>Resuming from paused.</summary>
    ContinuePending = 6,

    /// <summary>Pausing.</summary>
    PausePending = 7,

    /// <summary>Paused.</summary>
    Paused = 8,

    /// <summary>
    /// The query itself failed — no rights, a broken service database, a
    /// remote machine. Distinct from <see cref="NotInstalled"/>, because
    /// "I could not look" and "it is not there" are different answers.
    /// </summary>
    QueryFailed = 9,
}

/// <summary>What the shell found when it asked about the Windows service.</summary>
public sealed record ServiceProbe
{
    /// <summary>Before any query has been made.</summary>
    public static ServiceProbe NotChecked(string serviceName) => new()
    {
        ServiceName = serviceName,
        State = PlatformServiceState.Unknown,
    };

    public required string ServiceName { get; init; }

    public required PlatformServiceState State { get; init; }

    /// <summary>The service's display name, when the query returned one.</summary>
    public string? DisplayName { get; init; }

    /// <summary>Why the query failed, when it did.</summary>
    public string? Problem { get; init; }

    /// <summary>True once a query has actually been attempted.</summary>
    public bool Checked => State != PlatformServiceState.Unknown;

    /// <summary>
    /// The service exists. False for <see cref="PlatformServiceState.Unknown"/>
    /// and <see cref="PlatformServiceState.QueryFailed"/> as well as for
    /// <see cref="PlatformServiceState.NotInstalled"/> — in neither case has
    /// the shell established that there is one.
    /// </summary>
    public bool Installed => State
        is PlatformServiceState.Stopped
        or PlatformServiceState.StartPending
        or PlatformServiceState.Running
        or PlatformServiceState.StopPending
        or PlatformServiceState.ContinuePending
        or PlatformServiceState.PausePending
        or PlatformServiceState.Paused;

    public bool IsRunning => State == PlatformServiceState.Running;

    /// <summary>A start command would be meaningful right now.</summary>
    public bool CanStart => State is PlatformServiceState.Stopped or PlatformServiceState.Paused;

    /// <summary>A stop command would be meaningful right now.</summary>
    public bool CanStop => State is PlatformServiceState.Running or PlatformServiceState.Paused;

    /// <summary>Mid-transition; the right response is to wait and look again.</summary>
    public bool IsTransitioning => State
        is PlatformServiceState.StartPending
        or PlatformServiceState.StopPending
        or PlatformServiceState.ContinuePending
        or PlatformServiceState.PausePending;
}

/// <summary>How a start or stop request ended.</summary>
public enum ServiceControlResult
{
    /// <summary>The service reached the state that was asked for.</summary>
    Succeeded = 0,

    /// <summary>It did not reach that state inside the timeout.</summary>
    TimedOut = 1,

    /// <summary>Refused for want of rights. Elevation is the way through.</summary>
    AccessDenied = 2,

    /// <summary>There is no such service.</summary>
    NotInstalled = 3,

    /// <summary>It was already in the state asked for. Not a failure.</summary>
    AlreadyThere = 4,

    /// <summary>Anything else, reported verbatim.</summary>
    Failed = 5,
}

/// <summary>
/// The outcome of one service control request, with the sentence to show.
/// </summary>
public sealed record ServiceControlOutcome(
    ServiceControlResult Result,
    PlatformServiceState FinalState,
    TimeSpan Waited,
    string Message,
    bool SuggestElevation)
{
    public bool Worked => Result is ServiceControlResult.Succeeded or ServiceControlResult.AlreadyThere;
}

/// <summary>
/// Turns raw service-control-manager facts into states, sentences and outcomes.
/// </summary>
/// <remarks>
/// Everything here is a pure function of numbers and strings, so that the parts
/// that decide what an operator is told are testable without a service control
/// manager — which is exactly the part that is hard to exercise by hand, since
/// reproducing "access denied on start" or "start timed out" on a real machine
/// means deliberately breaking a homestead's control plane.
/// </remarks>
public static class ServiceStateInterpreter
{
    // Win32 error codes the service control manager actually returns.
    private const int ErrorAccessDenied = 5;
    private const int ErrorServiceAlreadyRunning = 1056;
    private const int ErrorServiceDoesNotExist = 1060;
    private const int ErrorServiceNotActive = 1062;
    private const int ErrorServiceRequestTimeout = 1053;

    /// <summary>
    /// Maps a <c>ServiceControllerStatus</c> value. Kept as an integer so this
    /// assembly never references <c>System.ServiceProcess</c>, and so the
    /// mapping is exercised without one.
    /// </summary>
    public static PlatformServiceState FromServiceControllerStatus(int status) => status switch
    {
        1 => PlatformServiceState.Stopped,
        2 => PlatformServiceState.StartPending,
        3 => PlatformServiceState.StopPending,
        4 => PlatformServiceState.Running,
        5 => PlatformServiceState.ContinuePending,
        6 => PlatformServiceState.PausePending,
        7 => PlatformServiceState.Paused,

        // A status this shell does not recognise is never rounded to Running.
        _ => PlatformServiceState.QueryFailed,
    };

    /// <summary>One line for the launcher's service check.</summary>
    public static string Describe(ServiceProbe probe)
    {
        ArgumentNullException.ThrowIfNull(probe);

        return probe.State switch
        {
            PlatformServiceState.Unknown =>
                $"Not checked yet.",

            PlatformServiceState.NotInstalled =>
                $"No Windows service called '{probe.ServiceName}' is installed on this machine. "
                + "That is normal for a development checkout or a portable copy; on a homestead "
                + "node it means nothing keeps the platform running when nobody is signed in.",

            PlatformServiceState.Stopped =>
                $"Installed and stopped. Starting it hands the platform to Windows, which keeps it "
                + "running after this shell closes.",

            PlatformServiceState.StartPending =>
                "Starting. Windows has accepted the start request and the service has not finished "
                + "coming up yet.",

            PlatformServiceState.Running =>
                "Running. The platform is being kept up by Windows, independently of this shell.",

            PlatformServiceState.StopPending =>
                "Stopping.",

            PlatformServiceState.ContinuePending =>
                "Resuming from paused.",

            PlatformServiceState.PausePending =>
                "Pausing.",

            PlatformServiceState.Paused =>
                "Paused. A paused platform is not controlling anything.",

            _ => probe.Problem is { Length: > 0 }
                ? $"The service could not be queried: {probe.Problem}. This says nothing about whether "
                  + "the platform is running — only that this shell could not ask."
                : "The service could not be queried. This says nothing about whether the platform is "
                  + "running — only that this shell could not ask.",
        };
    }

    /// <summary>
    /// Reads the outcome of a start request.
    /// </summary>
    /// <param name="finalState">State observed after waiting.</param>
    /// <param name="waited">How long the shell actually waited.</param>
    /// <param name="timeout">The budget it was given.</param>
    /// <param name="win32Error">Win32 code from the failure, if there was one.</param>
    /// <param name="errorMessage">The exception message, if there was one.</param>
    /// <param name="serviceName">For the wording.</param>
    public static ServiceControlOutcome InterpretStart(
        PlatformServiceState finalState,
        TimeSpan waited,
        TimeSpan timeout,
        int? win32Error = null,
        string? errorMessage = null,
        string serviceName = ShellMessages.ServiceName)
    {
        if (win32Error == ErrorServiceAlreadyRunning || (win32Error is null && finalState == PlatformServiceState.Running && waited == TimeSpan.Zero))
        {
            return new ServiceControlOutcome(
                ServiceControlResult.AlreadyThere,
                PlatformServiceState.Running,
                waited,
                $"The service '{serviceName}' was already running.",
                SuggestElevation: false);
        }

        var refusal = Refusal(win32Error, errorMessage, serviceName, "start", finalState, waited);
        if (refusal is not null)
        {
            return refusal;
        }

        if (finalState == PlatformServiceState.Running)
        {
            return new ServiceControlOutcome(
                ServiceControlResult.Succeeded,
                finalState,
                waited,
                $"The service '{serviceName}' started in {RelativeTime.Describe(waited)}. The platform "
                + "now runs under Windows and keeps running when this shell closes.",
                SuggestElevation: false);
        }

        // The honest reading of a timeout: Windows may still bring it up. The
        // shell says what it observed and what it did not, and does not
        // pronounce the start failed.
        return new ServiceControlOutcome(
            ServiceControlResult.TimedOut,
            finalState,
            waited,
            $"The service '{serviceName}' did not reach Running within "
            + $"{RelativeTime.Describe(timeout)} — it is {Word(finalState)}. Windows may still be "
            + "starting it; the shell stopped waiting, it did not cancel the start. Check the "
            + "platform log before starting it again.",
            SuggestElevation: false);
    }

    /// <summary>Reads the outcome of a stop request.</summary>
    public static ServiceControlOutcome InterpretStop(
        PlatformServiceState finalState,
        TimeSpan waited,
        TimeSpan timeout,
        int? win32Error = null,
        string? errorMessage = null,
        string serviceName = ShellMessages.ServiceName)
    {
        if (win32Error == ErrorServiceNotActive
            || (win32Error is null && finalState == PlatformServiceState.Stopped && waited == TimeSpan.Zero))
        {
            return new ServiceControlOutcome(
                ServiceControlResult.AlreadyThere,
                PlatformServiceState.Stopped,
                waited,
                $"The service '{serviceName}' was already stopped.",
                SuggestElevation: false);
        }

        var refusal = Refusal(win32Error, errorMessage, serviceName, "stop", finalState, waited);
        if (refusal is not null)
        {
            return refusal;
        }

        if (finalState == PlatformServiceState.Stopped)
        {
            return new ServiceControlOutcome(
                ServiceControlResult.Succeeded,
                finalState,
                waited,
                $"The service '{serviceName}' stopped in {RelativeTime.Describe(waited)}. THE PLATFORM "
                + "IS NOW STOPPED: nothing is being controlled and no alarms are being evaluated on "
                + "this node.",
                SuggestElevation: false);
        }

        return new ServiceControlOutcome(
            ServiceControlResult.TimedOut,
            finalState,
            waited,
            $"The service '{serviceName}' did not stop within {RelativeTime.Describe(timeout)} — it is "
            + $"{Word(finalState)}. Do not assume the platform has stopped or that it is still "
            + "controlling; check the platform log.",
            SuggestElevation: false);
    }

    private static ServiceControlOutcome? Refusal(
        int? win32Error,
        string? errorMessage,
        string serviceName,
        string verb,
        PlatformServiceState finalState,
        TimeSpan waited)
    {
        if (win32Error == ErrorAccessDenied)
        {
            return new ServiceControlOutcome(
                ServiceControlResult.AccessDenied,
                finalState,
                waited,
                $"Windows refused to {verb} the service '{serviceName}': this account does not have "
                + "the right to control it. Restarting the shell as an administrator will let it try.",
                SuggestElevation: true);
        }

        if (win32Error == ErrorServiceDoesNotExist)
        {
            return new ServiceControlOutcome(
                ServiceControlResult.NotInstalled,
                PlatformServiceState.NotInstalled,
                waited,
                $"There is no Windows service called '{serviceName}' on this machine, so there was "
                + $"nothing to {verb}. Check the name in Settings, or start the platform from its "
                + "executable instead.",
                SuggestElevation: false);
        }

        if (win32Error == ErrorServiceRequestTimeout)
        {
            return new ServiceControlOutcome(
                ServiceControlResult.TimedOut,
                finalState,
                waited,
                $"Windows reported that the service '{serviceName}' did not respond to the {verb} "
                + "request in time. It may still be working; check the platform log before trying again.",
                SuggestElevation: false);
        }

        if (win32Error is not null || !string.IsNullOrWhiteSpace(errorMessage))
        {
            var detail = string.IsNullOrWhiteSpace(errorMessage)
                ? $"Windows error {win32Error}"
                : errorMessage!.Trim();

            // Access-denied wording arrives in several shapes through the
            // service controller wrapper; catch it by text as well as by code
            // so the operator is offered elevation rather than a raw message.
            var denied = detail.Contains("access is denied", StringComparison.OrdinalIgnoreCase)
                || detail.Contains("access denied", StringComparison.OrdinalIgnoreCase);

            return new ServiceControlOutcome(
                denied ? ServiceControlResult.AccessDenied : ServiceControlResult.Failed,
                finalState,
                waited,
                denied
                    ? $"Windows refused to {verb} the service '{serviceName}': {detail}. Restarting the "
                      + "shell as an administrator will let it try."
                    : $"The shell could not {verb} the service '{serviceName}': {detail}",
                SuggestElevation: denied);
        }

        return null;
    }

    private static string Word(PlatformServiceState state) => state switch
    {
        PlatformServiceState.NotInstalled => "not installed",
        PlatformServiceState.Stopped => "stopped",
        PlatformServiceState.StartPending => "still starting",
        PlatformServiceState.Running => "running",
        PlatformServiceState.StopPending => "still stopping",
        PlatformServiceState.ContinuePending => "resuming",
        PlatformServiceState.PausePending => "pausing",
        PlatformServiceState.Paused => "paused",
        PlatformServiceState.QueryFailed => "in a state the shell could not read",
        _ => "in an unknown state",
    };
}
