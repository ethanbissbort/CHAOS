namespace Chaos.Shell.Core;

/// <summary>
/// Decides what the launcher window says and offers, from the facts it was
/// given.
/// </summary>
/// <remarks>
/// <para>
/// This is the whole of the startup experience's judgement, kept in one pure
/// function so that every awkward combination — a service that is running while
/// the gateway is silent, a gateway that answers while its backend is dead, a
/// host too old to report setup — can be exercised without Windows, a service
/// control manager or a network.
/// </para>
/// <para>
/// Two rules run through all of it.
/// </para>
/// <para>
/// First: never show a calm state that has not been verified. Every check
/// starts <see cref="CheckState.Unknown"/> and only becomes
/// <see cref="CheckState.Pass"/> on evidence. "The shell cannot see it" is
/// never written as "it is not running".
/// </para>
/// <para>
/// Second: never offer an action that cannot be taken, and never withhold one
/// silently. Every button is either pressable or carries the sentence saying why
/// it is not.
/// </para>
/// </remarks>
public static class LauncherStateMachine
{
    public static LauncherView Evaluate(LauncherFacts facts)
    {
        ArgumentNullException.ThrowIfNull(facts);

        var plan = PlatformStartPlanner.Plan(facts);
        var setup = SetupPresenter.Present(facts.Setup);
        var checks = BuildChecks(facts, plan, setup);
        var phase = Phase(facts, plan);
        var banner = RunModeBanner.For(
            facts.RunMode, facts.Settings.ServiceName, facts.Endpoints.BaseUri.ToString());

        var (headline, summary) = Narrate(phase, facts, plan, setup, banner);
        var actions = BuildActions(phase, facts, plan, setup);

        return new LauncherView(
            phase,
            headline,
            summary,
            checks,
            actions,
            banner,
            setup,
            ShowProgress: facts.ActionInProgress
                || phase is LauncherPhase.Checking or LauncherPhase.Starting or LauncherPhase.SettingUp,
            ConsoleIsUsable: facts.Gateway.Reachable,
            Notices: Notices(facts))
        {
            SafeToAdvanceUnattended = phase == LauncherPhase.Ready && MayAdvance(facts),
        };
    }

    /// <summary>
    /// Whether the one unverified thing, if any, is a gap this shell
    /// understands.
    /// </summary>
    private static bool MayAdvance(LauncherFacts facts) => facts.Setup.Availability switch
    {
        // Verified set up.
        SetupAvailability.Available => facts.Setup.State == SetupState.Ready,

        // A gateway older than this shell cannot report setup. That is a known
        // gap with a known explanation, and it is not a reason to keep an
        // operator on a start screen every morning.
        SetupAvailability.NotSupportedByHost => true,

        // Unreadable, unreachable or not yet asked: something is odd, and the
        // screen that says so stays up.
        _ => false,
    };

    // ------------------------------------------------------------------ phase --

    private static LauncherPhase Phase(LauncherFacts facts, PlatformStartPlan plan)
    {
        if (!facts.Gateway.Attempted)
        {
            return LauncherPhase.Checking;
        }

        if (facts.Gateway.Reachable)
        {
            return facts.Gateway.Backend switch
            {
                BackendState.Starting => LauncherPhase.Starting,
                BackendState.Down => LauncherPhase.BackendDown,

                // An unknown backend state is not a reason to hold the console
                // back — the gateway is answering and serving it — but it is
                // not "ready" either, and the check row says so.
                _ => SetupPhase(facts),
            };
        }

        if (facts.ActionInProgress
            || facts.Service.State == PlatformServiceState.StartPending
            || facts.ManagedChildRunning)
        {
            return LauncherPhase.Starting;
        }

        // Nothing answered. Is there anything the operator could press?
        return plan.CanStart ? LauncherPhase.PlatformDown : LauncherPhase.Blocked;
    }

    private static LauncherPhase SetupPhase(LauncherFacts facts) => facts.Setup.Availability switch
    {
        SetupAvailability.Available when facts.Setup.IsBusy => LauncherPhase.SettingUp,
        SetupAvailability.Available when facts.Setup.NeedsOperator => LauncherPhase.SetupRequired,
        SetupAvailability.Available when facts.Setup.State == SetupState.Ready => LauncherPhase.Ready,

        // Unknown, unreadable, or a host too old to say. The gateway and the
        // backend are both up, so the console is usable; the setup row carries
        // the caveat rather than the whole screen refusing to move on.
        _ => LauncherPhase.Ready,
    };

    // ----------------------------------------------------------------- checks --

    private static IReadOnlyList<LauncherCheck> BuildChecks(
        LauncherFacts facts, PlatformStartPlan plan, SetupView setup)
    {
        var address = facts.Endpoints.BaseUri.ToString();

        return new[]
        {
            GatewayCheck(facts, address),
            ServiceCheck(facts),
            ExecutableCheck(facts, plan),
            BackendCheck(facts),
            SetupCheck(setup),
        };
    }

    private static LauncherCheck GatewayCheck(LauncherFacts facts, string address)
    {
        var probe = facts.Gateway;
        var label = $"Gateway at {address}";

        if (!probe.Attempted)
        {
            return new LauncherCheck(LauncherCheckId.Gateway, label, CheckState.Checking,
                "Contacting the gateway…");
        }

        if (probe.Reachable)
        {
            var version = string.IsNullOrWhiteSpace(probe.HostVersion)
                ? "version not reported"
                : $"version {probe.HostVersion}";

            return new LauncherCheck(LauncherCheckId.Gateway, label, CheckState.Pass,
                $"Answering ({version}).");
        }

        var error = string.IsNullOrWhiteSpace(probe.Error) ? "no answer" : probe.Error!.Trim();
        var attempts = probe.ConsecutiveFailures <= 1
            ? string.Empty
            : $" after {probe.ConsecutiveFailures} attempts";

        return new LauncherCheck(LauncherCheckId.Gateway, label, CheckState.Fail,
            $"No answer{attempts}: {error}. This means the shell cannot see the platform; it does not "
            + "by itself mean the platform has stopped.");
    }

    private static LauncherCheck ServiceCheck(LauncherFacts facts)
    {
        var probe = facts.Service;
        var label = $"Windows service '{probe.ServiceName}'";
        var detail = ServiceStateInterpreter.Describe(probe);

        var state = probe.State switch
        {
            PlatformServiceState.Unknown => CheckState.Checking,
            PlatformServiceState.Running => CheckState.Pass,

            // Not installed is not a fault — a development checkout or portable
            // copy is a legitimate way to run this. It is a warning, because on
            // a homestead node it means nothing survives a sign-out.
            PlatformServiceState.NotInstalled => CheckState.Warn,
            PlatformServiceState.Stopped => CheckState.Fail,
            PlatformServiceState.Paused => CheckState.Fail,
            PlatformServiceState.QueryFailed => CheckState.Unknown,
            _ => CheckState.Checking,
        };

        return new LauncherCheck(LauncherCheckId.WindowsService, label, state, detail);
    }

    private static LauncherCheck ExecutableCheck(LauncherFacts facts, PlatformStartPlan plan)
    {
        const string Label = "Platform executable";

        // When a running service already accounts for the platform, hunting for
        // an executable is noise — but the row still says why it is quiet.
        if (facts.Service.IsRunning && plan.Method != StartMethod.ManagedChild)
        {
            return new LauncherCheck(LauncherCheckId.HostExecutable, Label, CheckState.NotApplicable,
                $"Not needed: the Windows service '{facts.Service.ServiceName}' is running the platform.");
        }

        if (!facts.Executable.Searched)
        {
            return new LauncherCheck(LauncherCheckId.HostExecutable, Label, CheckState.Checking,
                $"Looking for {HostExecutableLocator.FileName}…");
        }

        var detail = HostExecutableLocator.Describe(facts.Executable);

        if (!facts.Executable.Found)
        {
            // Only a failure if it would otherwise have been the way forward.
            var state = facts.Service.Installed ? CheckState.Warn : CheckState.Fail;
            return new LauncherCheck(LauncherCheckId.HostExecutable, Label, state, detail);
        }

        return new LauncherCheck(
            LauncherCheckId.HostExecutable,
            Label,
            facts.Executable.Problem is { Length: > 0 } ? CheckState.Warn : CheckState.Pass,
            detail);
    }

    private static LauncherCheck BackendCheck(LauncherFacts facts)
    {
        const string Label = "Platform backend";

        if (!facts.Gateway.Reachable)
        {
            return new LauncherCheck(LauncherCheckId.Backend, Label, CheckState.Unknown,
                "Unknown: the backend is reported by the gateway, and the gateway is not answering. "
                + "The shell has no second way to ask.");
        }

        var sentence = facts.Gateway.Health?.BackendDetail;
        var suffix = string.IsNullOrWhiteSpace(sentence) ? string.Empty : $" {sentence!.Trim()}";

        return facts.Gateway.Backend switch
        {
            BackendState.Up => new LauncherCheck(LauncherCheckId.Backend, Label, CheckState.Pass,
                "The gateway reports the Python backend is up." + suffix),

            BackendState.Starting => new LauncherCheck(LauncherCheckId.Backend, Label, CheckState.Checking,
                "The gateway reports the Python backend is still starting. A cold start on a low-power "
                + "node takes a minute or so." + suffix),

            BackendState.Down => new LauncherCheck(LauncherCheckId.Backend, Label, CheckState.Fail,
                "The gateway reports the Python backend is DOWN. The gateway itself is answering, so "
                + "the console will load, but control, alarm evaluation and history are not running."
                + suffix),

            _ => new LauncherCheck(LauncherCheckId.Backend, Label, CheckState.Unknown,
                "The gateway did not say what state the backend is in, so the shell does not know."
                + suffix),
        };
    }

    private static LauncherCheck SetupCheck(SetupView view)
    {
        return new LauncherCheck(
            LauncherCheckId.Setup,
            "First-run setup",
            view.OverallState,
            $"{view.Headline}. {view.Summary}");
    }

    // ---------------------------------------------------------------- wording --

    private static (string Headline, string Summary) Narrate(
        LauncherPhase phase,
        LauncherFacts facts,
        PlatformStartPlan plan,
        SetupView setup,
        RunModeBanner banner)
    {
        var address = facts.Endpoints.BaseUri.ToString();

        return phase switch
        {
            LauncherPhase.Checking => (
                "Starting Project CHAOS",
                $"Looking for the platform at {address}."),

            LauncherPhase.Starting => Starting(facts, address),

            LauncherPhase.PlatformDown => (
                "The platform is not answering",
                $"Nothing answered at {address}. {banner.Detail} {plan.Explanation}"),

            LauncherPhase.Blocked => (
                "The platform is not answering, and this shell cannot start it",
                $"Nothing answered at {address}. {plan.Refusal} "
                + "The platform may still be running somewhere this shell cannot see — check the node "
                + "directly before assuming it has stopped."),

            LauncherPhase.BackendDown => (
                "The platform is only half up",
                $"The gateway at {address} is answering, but it reports that the Python backend behind "
                + "it is down. Nothing is being controlled and no alarms are being evaluated. The "
                + "console will open, but it will have nothing to show."),

            LauncherPhase.SetupRequired => (
                facts.Setup.State switch
                {
                    SetupState.NotStarted => "This platform has not been set up yet",
                    SetupState.Failed => "Setup did not finish",
                    _ => "Setup needs attention",
                },
                setup.Summary),

            LauncherPhase.SettingUp => (
                "Setting up",
                setup.Summary),

            LauncherPhase.Ready => (
                "Project CHAOS is ready",
                $"The platform is answering at {address} and reports it is set up. {banner.Headline}."),

            _ => ("Project CHAOS", $"Looking for the platform at {address}."),
        };
    }

    private static (string, string) Starting(LauncherFacts facts, string address)
    {
        if (facts.ActionInProgress && facts.ActionInProgressMessage is { Length: > 0 } message)
        {
            return ("Starting the platform", message);
        }

        if (facts.Gateway.Reachable && facts.Gateway.Backend == BackendState.Starting)
        {
            return (
                "The platform is coming up",
                $"The gateway at {address} is answering and reports its backend is still starting. "
                + "A first start on a low-power node takes a minute or so.");
        }

        if (facts.Service.State == PlatformServiceState.StartPending)
        {
            return (
                "The platform is starting",
                $"Windows reports the service '{facts.Service.ServiceName}' is starting. The shell is "
                + $"waiting for the gateway at {address} to answer.");
        }

        return (
            "The platform is starting",
            $"This shell started the platform and is waiting for the gateway at {address} to answer.");
    }

    // ---------------------------------------------------------------- actions --

    private static IReadOnlyList<LauncherActionOffer> BuildActions(
        LauncherPhase phase,
        LauncherFacts facts,
        PlatformStartPlan plan,
        SetupView setup)
    {
        var offers = new List<LauncherActionOffer>(7);

        var startIsPrimary = phase is LauncherPhase.PlatformDown or LauncherPhase.Blocked;
        var setupIsPrimary = phase == LauncherPhase.SetupRequired && setup.RunIsPrimary;
        var consoleIsPrimary = phase == LauncherPhase.Ready;

        offers.Add(StartOffer(facts, plan, startIsPrimary));

        if (setup.RunLabel is { Length: > 0 })
        {
            offers.Add(SetupOffer(facts, setup, setupIsPrimary));
        }
        else
        {
            offers.Add(new LauncherActionOffer(
                LauncherAction.RunSetup,
                SetupPresenter.SetUpNow,
                ActionAvailability.Unavailable,
                SetupUnavailableReason(facts),
                IsPrimary: false));
        }

        offers.Add(ConsoleOffer(facts, phase, consoleIsPrimary));

        if (StopOffer(facts) is { } stop)
        {
            offers.Add(stop);
        }

        offers.Add(new LauncherActionOffer(
            LauncherAction.OpenSettings,
            "Settings",
            ActionAvailability.Available,
            "Gateway address, how the platform is started, where its data lives, theme and the "
            + "annunciator's display.",
            IsPrimary: false));

        offers.Add(new LauncherActionOffer(
            LauncherAction.OpenLogs,
            "Open logs",
            ActionAvailability.Available,
            "Opens the folder holding the shell and platform logs.",
            IsPrimary: false));

        if (ElevationOffer(facts, plan) is { } elevate)
        {
            offers.Add(elevate);
        }

        offers.Add(new LauncherActionOffer(
            LauncherAction.RecheckNow,
            "Check again",
            facts.ActionInProgress ? ActionAvailability.InProgress : ActionAvailability.Available,
            facts.ActionInProgress
                ? "Something the shell was asked to do is still running."
                : "Repeats every check now instead of waiting for the next poll.",
            IsPrimary: false));

        // The primary action leads. Everything else keeps the order above,
        // which is the order an operator works through the screen.
        return offers.OrderByDescending(o => o.IsPrimary).ToList();
    }

    private static LauncherActionOffer StartOffer(
        LauncherFacts facts,
        PlatformStartPlan plan,
        bool isPrimary)
    {
        if (facts.ActionInProgress)
        {
            return new LauncherActionOffer(
                LauncherAction.StartPlatform, plan.ButtonLabel, ActionAvailability.InProgress,
                facts.ActionInProgressMessage ?? "A start is already running.", IsPrimary: false);
        }

        if (!plan.CanStart)
        {
            return new LauncherActionOffer(
                LauncherAction.StartPlatform, plan.ButtonLabel, ActionAvailability.Unavailable,
                plan.Refusal ?? "There is no way to start the platform from here.", IsPrimary: false);
        }

        if (facts.Gateway.Reachable)
        {
            return new LauncherActionOffer(
                LauncherAction.StartPlatform, plan.ButtonLabel, ActionAvailability.Unavailable,
                $"Something is already answering at {facts.Endpoints.BaseUri}. Starting another "
                + "platform on the same address would fail on the port, so this is not offered.",
                IsPrimary: false);
        }

        var availability = plan.Elevation.Need switch
        {
            ElevationNeed.Required => ActionAvailability.NeedsElevation,
            ElevationNeed.Advisory => ActionAvailability.NeedsElevation,
            _ => ActionAvailability.Available,
        };

        return new LauncherActionOffer(
            LauncherAction.StartPlatform, plan.ButtonLabel, availability, plan.Explanation, isPrimary);
    }

    private static LauncherActionOffer SetupOffer(LauncherFacts facts, SetupView setup, bool isPrimary)
    {
        if (facts.ActionInProgress)
        {
            return new LauncherActionOffer(
                LauncherAction.RunSetup, setup.RunLabel!, ActionAvailability.InProgress,
                facts.ActionInProgressMessage ?? "Setup is running.", IsPrimary: false);
        }

        return new LauncherActionOffer(
            LauncherAction.RunSetup,
            setup.RunLabel!,
            ActionAvailability.Available,
            setup.RunLabel == SetupPresenter.RunAgain
                ? "Re-checks and repairs anything the platform needs. Safe to run on a working system."
                : "Creates the database and loads the site's points, bindings, alarm definitions and "
                  + "load schedule. The platform does the work; this button asks it to.",
            isPrimary);
    }

    private static string SetupUnavailableReason(LauncherFacts facts) => facts.Setup.Availability switch
    {
        SetupAvailability.NotChecked =>
            "The shell has not been able to ask the platform about setup yet.",

        SetupAvailability.Unreachable =>
            "Setup is run by the platform, and the shell cannot reach it. Start the platform first.",

        SetupAvailability.NotSupportedByHost =>
            "This gateway is older than this shell and does not offer a setup endpoint, so the shell "
            + "cannot run setup for you. Update the platform, or run setup the way that build "
            + "documents.",

        SetupAvailability.Unreadable =>
            "The platform answered about setup in a form this shell could not read, so the shell will "
            + "not send it a run command it cannot reason about.",

        _ when facts.Setup.IsBusy =>
            "Setup is already running on the platform.",

        // Available, but the platform used a state word this shell does not
        // know. Sending it a run command whose effect cannot be reasoned about
        // is worse than saying so.
        _ => "The platform reported a setup state this shell does not recognise"
            + (facts.Setup.RawState is { Length: > 0 } word ? $" ('{word}')" : string.Empty)
            + ", so the shell will not send it a setup command.",
    };

    private static LauncherActionOffer ConsoleOffer(
        LauncherFacts facts,
        LauncherPhase phase,
        bool isPrimary)
    {
        if (!facts.Gateway.Reachable)
        {
            return new LauncherActionOffer(
                LauncherAction.OpenConsole, "Open console", ActionAvailability.Unavailable,
                $"Nothing is answering at {facts.Endpoints.BaseUri}, so the console has nothing to "
                + "load. Opening it would show a blank window, which would tell you nothing.",
                IsPrimary: false);
        }

        var caveat = phase switch
        {
            LauncherPhase.BackendDown =>
                "The gateway serves the console, but its backend is down, so the console will show no "
                + "live data.",

            LauncherPhase.SetupRequired =>
                "The platform is answering, but it is not set up. The console will be largely empty "
                + "until setup has run.",

            LauncherPhase.SettingUp =>
                "Setup is still running, so parts of the console may be incomplete until it finishes.",

            LauncherPhase.Starting =>
                "The backend is still coming up, so the console may be incomplete for a moment.",

            _ => "Shows the operator console for this platform.",
        };

        return new LauncherActionOffer(
            LauncherAction.OpenConsole, "Open console", ActionAvailability.Available, caveat, isPrimary);
    }

    private static LauncherActionOffer? StopOffer(LauncherFacts facts)
    {
        if (facts.ManagedChildRunning)
        {
            return new LauncherActionOffer(
                LauncherAction.StopPlatform,
                "Stop platform",
                facts.ActionInProgress ? ActionAvailability.InProgress : ActionAvailability.Available,
                "Stops the platform this shell started. THE HOMESTEAD WILL STOP BEING CONTROLLED and "
                + "alarms will stop being evaluated until it is started again.",
                IsPrimary: false);
        }

        if (facts.Service.CanStop)
        {
            var elevation = facts.ElevationRefused
                ? Elevation.AfterAccessDenied(facts.IsElevated, $"stop the service '{facts.Service.ServiceName}'")
                : Elevation.ForServiceControl(facts.IsElevated, "stop", facts.Service.ServiceName);

            return new LauncherActionOffer(
                LauncherAction.StopPlatform,
                "Stop platform",
                facts.ActionInProgress
                    ? ActionAvailability.InProgress
                    : elevation.Need == ElevationNeed.NotRequired
                        ? ActionAvailability.Available
                        : ActionAvailability.NeedsElevation,
                $"Stops the Windows service '{facts.Service.ServiceName}'. THE HOMESTEAD WILL STOP "
                + "BEING CONTROLLED and alarms will stop being evaluated until it is started again. "
                + (elevation.Need == ElevationNeed.NotRequired ? string.Empty : elevation.Explanation),
                IsPrimary: false);
        }

        // Nothing this shell can stop. Say so only when something is running,
        // so the button does not appear on a dead platform.
        if (facts.Gateway.Reachable)
        {
            return new LauncherActionOffer(
                LauncherAction.StopPlatform,
                "Stop platform",
                ActionAvailability.Unavailable,
                "This shell did not start what is answering at this address and no Windows service "
                + "accounts for it, so it has no way to stop it. Whatever started it has to stop it.",
                IsPrimary: false);
        }

        return null;
    }

    private static LauncherActionOffer? ElevationOffer(LauncherFacts facts, PlatformStartPlan plan)
    {
        if (facts.IsElevated)
        {
            return null;
        }

        if (!facts.ElevationRefused && plan.Elevation.Need != ElevationNeed.Required)
        {
            return null;
        }

        return new LauncherActionOffer(
            LauncherAction.RelaunchElevated,
            Elevation.RelaunchLabel,
            ActionAvailability.Available,
            "Closes this shell and opens it again with administrator rights, so it can control the "
            + "Windows service. The platform is not touched by this: whatever is running keeps running.",
            IsPrimary: false);
    }

    // ---------------------------------------------------------------- notices --

    private static IReadOnlyList<string> Notices(LauncherFacts facts)
    {
        var notices = new List<string>(facts.Notices.Count + 2);
        notices.AddRange(facts.Notices);

        if (facts.LastActionMessage is { Length: > 0 } last)
        {
            notices.Add(last);
        }

        if (facts.Executable.Problem is { Length: > 0 } problem
            && facts.Executable.Found)
        {
            notices.Add(problem);
        }

        return notices;
    }
}

/// <summary>
/// Whether the launcher should come back after the console is already showing.
/// </summary>
/// <remarks>
/// The launcher is not only a startup screen: it is where an operator goes when
/// the platform stops being usable. But shoving it in front of a working console
/// on one dropped poll would be its own fault, so it takes a run of failures,
/// and it does not reappear immediately after being dismissed.
/// </remarks>
public static class LauncherReentry
{
    /// <summary>Failed polls before the console is judged to have lost the platform.</summary>
    public const int FailuresBeforeReturning = 3;

    /// <summary>Minimum gap between showing the launcher over a working console.</summary>
    public static readonly TimeSpan Quiet = TimeSpan.FromMinutes(2);

    /// <summary>
    /// Decides whether to bring the launcher back.
    /// </summary>
    /// <param name="consecutiveFailures">Failed probes since the last success.</param>
    /// <param name="sinceLastShown">
    /// Time since the launcher was last put in front of the operator, or null if
    /// it has not been since the console opened.
    /// </param>
    /// <param name="operatorDismissedIt">
    /// Whether the operator closed the launcher deliberately. Respected: an
    /// operator watching a known outage does not need the window pushed back at
    /// them every two minutes.
    /// </param>
    public static bool ShouldReturn(
        int consecutiveFailures,
        TimeSpan? sinceLastShown,
        bool operatorDismissedIt)
    {
        if (consecutiveFailures < FailuresBeforeReturning)
        {
            return false;
        }

        if (operatorDismissedIt)
        {
            return false;
        }

        return sinceLastShown is null || sinceLastShown >= Quiet;
    }
}
