using Chaos.Shell.Core;
using Microsoft.UI.Dispatching;
using Microsoft.UI.Text;
using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Controls;
using Microsoft.UI.Xaml.Media;
using Windows.Graphics;

namespace Chaos.Shell;

/// <summary>
/// The startup screen: what the shell found, and what can be done about it.
///
/// It runs before the console and comes back whenever the platform stops being
/// usable. Every word on it, and every decision about which buttons exist, comes
/// from <see cref="LauncherStateMachine"/> in Chaos.Shell.Core. This file gathers
/// facts from Windows, renders the view it is handed, and turns clicks back into
/// one operation at a time.
/// </summary>
public sealed partial class LauncherWindow : Window
{
    /// <summary>How often the checks are repeated while the launcher is open.</summary>
    private static readonly TimeSpan PollInterval = TimeSpan.FromSeconds(2);

    private readonly App _app;
    private readonly DispatcherQueue _dispatcher;
    private readonly CancellationTokenSource _closing = new();

    private GatewayProbe _gateway = GatewayProbe.NotProbed;
    private ServiceProbe _service;
    private HostExecutableProbe _executable = HostExecutableProbe.NotSearched;
    private SetupSnapshot _setup = SetupSnapshot.NotChecked;

    private LauncherView? _view;
    private bool _busy;
    private string? _busyMessage;
    private string? _lastActionMessage;
    private bool _elevationRefused;
    private bool _autoStartAttempted;
    private bool _operatorInteracted;
    private bool _logShown;

    public LauncherWindow(App app)
    {
        _app = app;
        _dispatcher = DispatcherQueue.GetForCurrentThread();
        _service = ServiceProbe.NotChecked(app.Settings.ServiceName);

        InitializeComponent();

        Title = "Project CHAOS — Start";
        AppWindow.Resize(new SizeInt32(1020, 900));

        // Render once from what is already known, so the window never appears
        // empty while the first probe is in flight.
        Render(LauncherStateMachine.Evaluate(CurrentFacts()));

        Closed += OnClosed;

        _ = RunAsync();
    }

    /// <summary>The operator closed the launcher deliberately.</summary>
    internal bool Dismissed { get; private set; }

    // -------------------------------------------------------------- facts --

    private LauncherFacts CurrentFacts() => new()
    {
        Endpoints = _app.Endpoints,
        Settings = _app.Settings,
        Gateway = _gateway,
        Service = _service,
        Executable = _executable,
        Setup = _setup,
        ManagedChildRunning = _app.Platform.ManagedChildRunning,
        IsElevated = _app.IsElevated,
        AddressIsThisMachine = _app.AddressIsThisMachine,
        ActionInProgress = _busy,
        ActionInProgressMessage = _busyMessage,
        LastActionMessage = _lastActionMessage,
        ElevationRefused = _elevationRefused,
        Notices = _app.StartupNotices,
    };

    private async Task RunAsync()
    {
        while (!_closing.IsCancellationRequested)
        {
            await RefreshAsync().ConfigureAwait(true);

            try
            {
                await Task.Delay(PollInterval, _closing.Token).ConfigureAwait(true);
            }
            catch (OperationCanceledException)
            {
                return;
            }
        }
    }

    private async Task RefreshAsync()
    {
        var settings = _app.Settings;

        _gateway = await _app.Probe
            .ProbeGatewayAsync(_app.Endpoints, _gateway, DateTimeOffset.UtcNow, _closing.Token)
            .ConfigureAwait(true);

        // The service control manager and the file system are both blocking
        // calls; they go to the pool so the window keeps painting.
        var serviceName = settings.ServiceName;
        var configuredExecutable = settings.HostExecutablePath;

        _service = await Task
            .Run(() => PlatformController.ProbeService(serviceName), _closing.Token)
            .ConfigureAwait(true);

        _executable = await Task
            .Run(() => PlatformController.LocateExecutable(configuredExecutable), _closing.Token)
            .ConfigureAwait(true);

        _setup = _gateway.Reachable
            ? await _app.Probe.ReadSetupAsync(_app.Endpoints, _closing.Token).ConfigureAwait(true)
            : SetupSnapshot.Unreachable(
                $"The shell cannot ask about setup because nothing is answering at {_app.Endpoints.BaseUri}.");

        var view = LauncherStateMachine.Evaluate(CurrentFacts());
        _app.NoteRunMode(view.RunMode.Mode);
        Render(view);

        await MaybeAutoStartAsync(view).ConfigureAwait(true);
        MaybeOpenConsole(view);
    }

    /// <summary>
    /// Starts the platform without being asked, once, when the operator has
    /// said they want that. It is attempted exactly once per launcher session
    /// so a platform that refuses to start is not restarted in a loop while
    /// nobody is watching.
    /// </summary>
    private async Task MaybeAutoStartAsync(LauncherView view)
    {
        if (_autoStartAttempted
            || _busy
            || !_app.Settings.AutoStartPlatform
            || view.Phase != LauncherPhase.PlatformDown)
        {
            return;
        }

        var start = view.Offer(LauncherAction.StartPlatform);
        if (start is null || !start.CanInvoke)
        {
            return;
        }

        _autoStartAttempted = true;
        await StartPlatformAsync().ConfigureAwait(true);
    }

    /// <summary>
    /// Moves on to the console once everything is verified — but only while the
    /// operator has not touched anything. Someone reading the launcher is
    /// working, and having the window replaced underneath them is not help.
    /// </summary>
    private void MaybeOpenConsole(LauncherView view)
    {
        if (_operatorInteracted || view.Phase != LauncherPhase.Ready)
        {
            return;
        }

        _app.ShowConsole();
        Close();
    }

    // ------------------------------------------------------------- render --

    private void Render(LauncherView view)
    {
        _view = view;

        HeadlineText.Text = view.Headline;
        SummaryText.Text = view.Summary;

        RunModeHeadline.Text = view.RunMode.Headline;
        RunModeDetail.Text = view.RunMode.Detail;

        var cautionary = view.RunMode.Severity == RunModeSeverity.Caution;
        RunModeBorder.BorderBrush = Brush(cautionary ? "ChaosShellWarn" : "ChaosShellStroke");
        RunModeHeadline.Foreground = Brush(cautionary ? "ChaosShellWarn" : "ChaosShellText");

        // The one distinction that must never be missed reads as an alarm when
        // this shell is the platform's parent process.
        RunModeBorder.BorderThickness = new Thickness(view.RunMode.ClosingTheShellStopsThePlatform ? 2 : 1);

        ProgressPanel.Visibility = view.ShowProgress ? Visibility.Visible : Visibility.Collapsed;
        ProgressText.Text = _busyMessage ?? view.Summary;

        RenderNotices(view);
        RenderChecks(view);
        RenderSetup(view);
        RenderActions(view);
        RenderLog();
    }

    private void RenderNotices(LauncherView view)
    {
        NoticeList.Children.Clear();

        foreach (var notice in view.Notices)
        {
            var border = new Border
            {
                Padding = new Thickness(12, 10, 12, 10),
                CornerRadius = new CornerRadius(4),
                BorderThickness = new Thickness(1),
                BorderBrush = Brush("ChaosShellWarn"),
                Child = new TextBlock
                {
                    Text = notice,
                    TextWrapping = TextWrapping.Wrap,
                    FontSize = 13,
                    Foreground = Brush("ChaosShellText"),
                },
            };

            NoticeList.Children.Add(border);
        }
    }

    private void RenderChecks(LauncherView view)
    {
        CheckList.Children.Clear();

        foreach (var check in view.Checks)
        {
            CheckList.Children.Add(Row(StateWord(check.State), check.State, check.Label, check.Detail));
        }
    }

    private void RenderSetup(LauncherView view)
    {
        SetupRows.Children.Clear();

        // The per-step breakdown is only worth the space once the platform has
        // actually told us something about it.
        var show = view.Setup.Rows.Count > 0;
        SetupPanel.Visibility = show ? Visibility.Visible : Visibility.Collapsed;

        if (!show)
        {
            return;
        }

        foreach (var row in view.Setup.Rows)
        {
            SetupRows.Children.Add(Row(StateWord(row.State), row.State, row.Label, row.Detail));
        }
    }

    /// <summary>
    /// One finding: a fixed-width state word, then the name, then the sentence.
    /// Words rather than glyphs — they are unambiguous, they read aloud, and
    /// they do not depend on an icon font being present.
    /// </summary>
    private Grid Row(string state, CheckState drawing, string label, string detail)
    {
        var grid = new Grid();
        grid.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(64) });
        grid.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });

        var stateText = new TextBlock
        {
            Text = state,
            FontFamily = new FontFamily("Consolas"),
            FontSize = 12,
            FontWeight = FontWeights.SemiBold,
            Foreground = Brush(ColourKey(drawing)),
            VerticalAlignment = VerticalAlignment.Top,
            Margin = new Thickness(0, 2, 0, 0),
        };
        Grid.SetColumn(stateText, 0);

        var body = new StackPanel { Spacing = 2 };
        body.Children.Add(new TextBlock
        {
            Text = label,
            FontSize = 14,
            FontWeight = FontWeights.SemiBold,
            TextWrapping = TextWrapping.Wrap,
            Foreground = Brush("ChaosShellText"),
        });
        body.Children.Add(new TextBlock
        {
            Text = detail,
            FontSize = 13,
            LineHeight = 19,
            TextWrapping = TextWrapping.Wrap,
            Foreground = Brush("ChaosShellMuted"),
        });
        Grid.SetColumn(body, 1);

        grid.Children.Add(stateText);
        grid.Children.Add(body);
        return grid;
    }

    private void RenderActions(LauncherView view)
    {
        ActionList.Children.Clear();

        foreach (var offer in view.Actions)
        {
            var grid = new Grid { Margin = new Thickness(0, 0, 0, 2) };
            grid.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(260) });
            grid.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });

            var button = new Button
            {
                Content = offer.Label,
                IsEnabled = offer.CanInvoke,
                MinWidth = 240,
                HorizontalAlignment = HorizontalAlignment.Left,
                Tag = offer.Action,
            };
            button.Click += OnActionClicked;

            if (offer.IsPrimary && Application.Current.Resources.TryGetValue("AccentButtonStyle", out var style)
                && style is Style accent)
            {
                button.Style = accent;
            }

            Grid.SetColumn(button, 0);

            // The reason is on the screen, not only in a tooltip. A control
            // that is greyed out with no explanation makes an operator's next
            // move a guess.
            var reason = new TextBlock
            {
                Text = offer.Reason,
                FontSize = 12,
                LineHeight = 18,
                TextWrapping = TextWrapping.Wrap,
                VerticalAlignment = VerticalAlignment.Center,
                Margin = new Thickness(12, 0, 0, 0),
                Foreground = Brush(offer.CanInvoke ? "ChaosShellMuted" : "ChaosShellWarn"),
            };
            Grid.SetColumn(reason, 1);

            grid.Children.Add(button);
            grid.Children.Add(reason);
            ActionList.Children.Add(grid);
        }
    }

    private void RenderLog()
    {
        var hasLog = _app.Platform.Log.Count > 0;
        LogToggle.Visibility = hasLog ? Visibility.Visible : Visibility.Collapsed;

        if (!hasLog)
        {
            _logShown = false;
            LogBorder.Visibility = Visibility.Collapsed;
            return;
        }

        LogToggle.Content = _logShown ? "Hide platform log" : "Show platform log";
        LogBorder.Visibility = _logShown ? Visibility.Visible : Visibility.Collapsed;

        if (_logShown)
        {
            LogText.Text = _app.Platform.Log.Render();
        }
    }

    private static string StateWord(CheckState state) => state switch
    {
        CheckState.Pass => "OK",
        CheckState.Warn => "WARN",
        CheckState.Fail => "FAIL",
        CheckState.Checking => "…",
        CheckState.Pending => "—",
        CheckState.NotApplicable => "n/a",
        _ => "UNKNOWN",
    };

    private static string ColourKey(CheckState state) => state switch
    {
        CheckState.Pass => "ChaosShellOk",
        CheckState.Warn => "ChaosShellWarn",
        CheckState.Fail => "ChaosShellError",
        CheckState.Checking => "ChaosShellAccent",
        _ => "ChaosShellMuted",
    };

    private static SolidColorBrush Brush(string key) =>
        Application.Current.Resources.TryGetValue(key, out var value) && value is SolidColorBrush brush
            ? brush
            : new SolidColorBrush(Microsoft.UI.Colors.Gray);

    // ------------------------------------------------------------ actions --

    private async void OnActionClicked(object sender, RoutedEventArgs e)
    {
        if (sender is not Button { Tag: LauncherAction action })
        {
            return;
        }

        _operatorInteracted = true;

        switch (action)
        {
            case LauncherAction.StartPlatform:
                await StartPlatformAsync().ConfigureAwait(true);
                break;

            case LauncherAction.StopPlatform:
                await StopPlatformAsync().ConfigureAwait(true);
                break;

            case LauncherAction.RunSetup:
                await RunSetupAsync().ConfigureAwait(true);
                break;

            case LauncherAction.OpenConsole:
                _app.ShowConsole();
                Close();
                break;

            case LauncherAction.OpenSettings:
                _app.ShowSettings();
                break;

            case LauncherAction.OpenLogs:
                _lastActionMessage = PlatformController.OpenLogFolder();
                break;

            case LauncherAction.RelaunchElevated:
                await RelaunchElevatedAsync().ConfigureAwait(true);
                break;

            case LauncherAction.RecheckNow:
                await RefreshAsync().ConfigureAwait(true);
                break;

            default:
                break;
        }

        if (!_closing.IsCancellationRequested)
        {
            Render(LauncherStateMachine.Evaluate(CurrentFacts()));
        }
    }

    private async Task StartPlatformAsync()
    {
        var plan = PlatformStartPlanner.Plan(CurrentFacts());
        if (!plan.CanStart)
        {
            _lastActionMessage = plan.Refusal;
            return;
        }

        Busy($"Starting the platform: {plan.Explanation}");

        try
        {
            if (plan.Method == StartMethod.WindowsService)
            {
                var outcome = await _app.Platform
                    .StartServiceAsync(plan.ServiceName!, _closing.Token)
                    .ConfigureAwait(true);

                _lastActionMessage = outcome.Message;
                _elevationRefused = outcome.Result == ServiceControlResult.AccessDenied;

                // Falling back to a managed child is offered, not taken: an
                // operator has to know they are choosing a platform that dies
                // with this window.
                if (_elevationRefused && _executable.Found)
                {
                    _lastActionMessage +=
                        " This shell can start the platform itself instead, but a platform started "
                        + "that way STOPS when you close this window. Turn off 'Prefer the Windows "
                        + "service' in Settings to do that.";
                }
            }
            else
            {
                _lastActionMessage = _app.Platform.StartManagedChild(plan)
                    ?? "The platform was started by this shell. IT WILL STOP WHEN YOU CLOSE THIS SHELL.";
            }
        }
        finally
        {
            Idle();
        }

        await RefreshAsync().ConfigureAwait(true);
    }

    private async Task StopPlatformAsync()
    {
        var facts = CurrentFacts();

        if (facts.ManagedChildRunning)
        {
            if (!await ConfirmAsync(
                    "Stop the platform?",
                    ShellMessages.ExitConfirmationFor(PlatformRunMode.ManagedByThisShell,
                        _app.Settings.ServiceName),
                    "Stop the platform").ConfigureAwait(true))
            {
                return;
            }

            Busy("Stopping the platform started by this shell.");
            try
            {
                _lastActionMessage = _app.Platform.StopManagedChild();
            }
            finally
            {
                Idle();
            }
        }
        else if (facts.Service.CanStop)
        {
            if (!await ConfirmAsync(
                    $"Stop the Windows service '{facts.Service.ServiceName}'?",
                    "THE HOMESTEAD WILL STOP BEING CONTROLLED. Alarm evaluation, logging and freeze "
                    + "protection all stop until the service is started again.",
                    "Stop the service").ConfigureAwait(true))
            {
                return;
            }

            Busy($"Stopping the Windows service '{facts.Service.ServiceName}'.");
            try
            {
                var outcome = await _app.Platform
                    .StopServiceAsync(facts.Service.ServiceName, _closing.Token)
                    .ConfigureAwait(true);

                _lastActionMessage = outcome.Message;
                _elevationRefused = outcome.Result == ServiceControlResult.AccessDenied;
            }
            finally
            {
                Idle();
            }
        }

        await RefreshAsync().ConfigureAwait(true);
    }

    private async Task RunSetupAsync()
    {
        Busy("Asking the platform to run first-run setup.");

        try
        {
            var result = await _app.Probe.RunSetupAsync(_app.Endpoints, _closing.Token).ConfigureAwait(true);
            _lastActionMessage = result.Message;
        }
        finally
        {
            Idle();
        }

        await RefreshAsync().ConfigureAwait(true);
    }

    private async Task RelaunchElevatedAsync()
    {
        // Restarting is an exit, so the exit warning applies verbatim — and it
        // is the one that says a platform started by this shell dies with it.
        var mode = _app.RunMode;
        var warning = ShellMessages.ExitConfirmationFor(mode, _app.Settings.ServiceName);

        if (!await ConfirmAsync(
                mode == PlatformRunMode.ManagedByThisShell
                    ? ShellMessages.ExitStopsPlatformTitle
                    : "Restart as administrator?",
                warning + " Windows will ask you to confirm.",
                Elevation.RelaunchLabel).ConfigureAwait(true))
        {
            return;
        }

        var problem = PlatformController.TryRelaunchElevated(_app.LaunchArguments);
        if (problem is not null)
        {
            _lastActionMessage = problem;
            return;
        }

        _app.ExitShell(skipConfirmation: true);
    }

    private void Busy(string message)
    {
        _busy = true;
        _busyMessage = message;
        _lastActionMessage = null;
        Render(LauncherStateMachine.Evaluate(CurrentFacts()));
    }

    private void Idle()
    {
        _busy = false;
        _busyMessage = null;
    }

    private async Task<bool> ConfirmAsync(string title, string body, string confirmLabel)
    {
        if (Content is not FrameworkElement root)
        {
            return false;
        }

        var dialog = new ContentDialog
        {
            XamlRoot = root.XamlRoot,
            Title = title,
            Content = new TextBlock { Text = body, TextWrapping = TextWrapping.Wrap },
            PrimaryButtonText = confirmLabel,
            CloseButtonText = "Cancel",

            // The safe answer is the default: nothing that stops a homestead's
            // control plane happens on a stray Enter key.
            DefaultButton = ContentDialogButton.Close,
        };

        return await dialog.ShowAsync() == ContentDialogResult.Primary;
    }

    private void OnToggleLog(object sender, RoutedEventArgs e)
    {
        _operatorInteracted = true;
        _logShown = !_logShown;
        RenderLog();
    }

    // ------------------------------------------------------------- window --

    /// <summary>Brings the launcher back in front of the console.</summary>
    internal void BringToFront()
    {
        AppWindow.Show();
        if (AppWindow.Presenter is Microsoft.UI.Windowing.OverlappedPresenter presenter)
        {
            presenter.Restore();
        }

        Activate();
    }

    private void OnClosed(object sender, WindowEventArgs args)
    {
        Dismissed = true;
        _closing.Cancel();
    }
}
