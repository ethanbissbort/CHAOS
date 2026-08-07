using Chaos.Shell.Core;
using Microsoft.UI.Dispatching;
using Microsoft.UI.Xaml;
using Microsoft.UI.Windowing;
using Windows.Graphics;

namespace Chaos.Shell;

/// <summary>
/// The operator console, hosted in WebView2 against the local gateway.
///
/// The window never shows an empty WebView2. It waits behind a gate panel until
/// <c>/health</c> answers, and if the budget runs out it shows a diagnostic
/// naming what was tried and what to do about it. A blank white window is the
/// worst possible outcome for a control system: it says nothing about whether
/// the platform is running.
///
/// Probing goes through Chaos.Shell.Core's PlatformProbeClient, so this window
/// and the launcher agree on what "reachable" means — in particular that a 503
/// from a gateway whose backend is down is still a reachable gateway.
/// </summary>
public sealed partial class MainWindow : Window
{
    private readonly App _app;
    private readonly DispatcherQueue _dispatcher;
    private readonly StartupSequence _startup;
    private readonly CancellationTokenSource _closing = new();

    private GatewayProbe _probe = GatewayProbe.NotProbed;
    private LinkStatus _link = LinkStatus.Connecting();
    private AlarmCounts _counts = AlarmCounts.None;
    private bool _consoleShown;

    public MainWindow(App app)
    {
        _app = app;
        _dispatcher = DispatcherQueue.GetForCurrentThread();
        _startup = new StartupSequence(app.Endpoints.Health);

        InitializeComponent();

        Title = "Project CHAOS — Operator Console";
        HostText.Text = app.Endpoints.BaseUri.ToString();

        ShowRunMode();
        RestorePlacement();

        Closed += OnClosed;

        _ = RunStartupAsync();
        _ = PollAsync();
    }

    /// <summary>
    /// Paints the run-mode strip. Called on every poll, because the answer can
    /// change under the window: a service can be stopped from Services.msc, and
    /// a platform this shell started can exit on its own.
    /// </summary>
    private void ShowRunMode()
    {
        var banner = RunModeBanner.For(
            _app.RunMode, _app.Settings.ServiceName, _app.Endpoints.BaseUri.ToString());

        RunModeText.Text = $"{banner.Headline} — {banner.Detail}";

        var cautionary = banner.Severity == RunModeSeverity.Caution;
        RunModeText.Foreground = App.Brush(cautionary ? "ChaosShellWarn" : "ChaosShellMuted");
        RunModeStrip.BorderBrush = App.Brush(
            banner.ClosingTheShellStopsThePlatform ? "ChaosShellWarn" : "ChaosShellStroke");
        RunModeStrip.BorderThickness = new Thickness(
            0, 0, 0, banner.ClosingTheShellStopsThePlatform ? 2 : 1);
    }

    private void RestorePlacement()
    {
        // WindowPlacementResolver refuses to restore onto a monitor that no
        // longer exists; a window placed off-screen is indistinguishable from
        // a shell that failed to start.
        var monitors = MonitorEnumerator.Current();
        if (monitors.Count == 0)
        {
            AppWindow.Resize(new SizeInt32(1440, 940));
            return;
        }

        var resolved = WindowPlacementResolver.Resolve(
            _app.Layout.Main, monitors, new ScreenRect(0, 0, 1440, 940));
        var b = resolved.Bounds;
        AppWindow.MoveAndResize(new RectInt32(b.Left, b.Top, b.Width, b.Height));
    }

    internal WindowPlacement? CurrentPlacement()
    {
        var pos = AppWindow.Position;
        var size = AppWindow.Size;
        return WindowPlacement.FromBounds(
            new ScreenRect(pos.X, pos.Y, size.Width, size.Height));
    }

    internal void BringToFront()
    {
        AppWindow.Show();
        if (AppWindow.Presenter is OverlappedPresenter p)
        {
            p.Restore();
        }
    }

    // ------------------------------------------------------------ startup --

    private async Task RunStartupAsync()
    {
        var attempt = 0;
        var started = DateTimeOffset.UtcNow;
        string? lastError = null;

        while (!_closing.IsCancellationRequested)
        {
            var elapsed = DateTimeOffset.UtcNow - started;
            var ok = await ProbeAsync().ConfigureAwait(true);
            var decision = _startup.Next(attempt, elapsed, ok, lastError);

            GateProgress.Value = decision.Fraction;
            GateProgressText.Text = decision.Progress;

            switch (decision.Action)
            {
                case StartupAction.Ready:
                    ShowConsole();
                    return;

                case StartupAction.GiveUp:
                    ShowDiagnostic(StartupDiagnostic.GatewayUnreachable(
                        _app.Endpoints, attempt, elapsed, lastError, PlatformController.LogDirectory,
                        _app.Settings.ServiceName));
                    return;

                case StartupAction.Wait:
                    attempt++;
                    try
                    {
                        await Task.Delay(decision.Delay, _closing.Token).ConfigureAwait(true);
                    }
                    catch (OperationCanceledException)
                    {
                        return;
                    }
                    break;

                default:
                    attempt++;
                    break;
            }

            lastError = _link.LastError;
        }
    }

    /// <summary>
    /// One probe. Reachability and the backend's state come from the shared
    /// client, so the console cannot disagree with the launcher about what it
    /// found.
    /// </summary>
    private async Task<bool> ProbeAsync()
    {
        try
        {
            _probe = await _app.Probe
                .ProbeGatewayAsync(_app.Endpoints, _probe, DateTimeOffset.UtcNow, _closing.Token)
                .ConfigureAwait(true);
        }
        catch (OperationCanceledException)
        {
            return false;
        }

        if (_probe.Reachable)
        {
            _link = LinkStatus.Online(DateTimeOffset.UtcNow, _probe.HostVersion);
            _app.NoteLinkRestored();
            return true;
        }

        _link = LinkStatus.Offline(_link.LastContactUtc, _probe.Error, _probe.ConsecutiveFailures);
        return false;
    }

    private void ShowConsole()
    {
        if (_consoleShown)
        {
            return;
        }

        try
        {
            ConsoleView.Source = _app.Endpoints.Console;
            ConsoleView.Visibility = Visibility.Visible;
            GatePanel.Visibility = Visibility.Collapsed;
            _consoleShown = true;
        }
        catch (Exception ex)
        {
            // Almost always the WebView2 runtime being absent. Say that, rather
            // than failing with a COM error the operator cannot act on.
            ShowDiagnostic(StartupDiagnostic.WebViewRuntimeMissing(_app.Endpoints, ex.Message));
        }
    }

    private void ShowDiagnostic(StartupDiagnostic diagnostic)
    {
        GateTitle.Text = diagnostic.Title;
        GateSummary.Text = diagnostic.Summary;
        GateFacts.ItemsSource = diagnostic.Facts
            .Select(f => $"{f.Label}: {f.Value}")
            .ToList();
        GateSteps.ItemsSource = diagnostic.NextSteps.ToList();
        GateProgress.Visibility = Visibility.Collapsed;
        RetryButton.Visibility = Visibility.Visible;
        StartScreenFromGateButton.Visibility = Visibility.Visible;
        LogsButton.Visibility = Visibility.Visible;
        GatePanel.Visibility = Visibility.Visible;
        ConsoleView.Visibility = Visibility.Collapsed;
    }

    // --------------------------------------------------------------- poll --

    /// <summary>
    /// One poller for the whole shell. The tray renders whatever this last saw,
    /// so the window and the tray icon can never disagree about platform state.
    /// </summary>
    private async Task PollAsync()
    {
        while (!_closing.IsCancellationRequested)
        {
            var ok = await ProbeAsync().ConfigureAwait(true);
            if (ok)
            {
                await ReadAlarmsAsync().ConfigureAwait(true);
            }
            else
            {
                // The launcher decides whether this warrants interrupting the
                // operator; the console only reports what it saw.
                _app.NoteLinkLost(_probe.ConsecutiveFailures);
            }

            LinkText.Text = _link.Phase switch
            {
                LinkPhase.Online => $"linked · {_link.PlatformVersion ?? "version unknown"}",
                LinkPhase.Connecting => "connecting…",
                _ => $"NO CONTACT · {_link.LastError}",
            };

            ShowRunMode();
            _app.UpdateTray(_link, _counts);

            try
            {
                await Task.Delay(TimeSpan.FromSeconds(5), _closing.Token).ConfigureAwait(true);
            }
            catch (OperationCanceledException)
            {
                return;
            }
        }
    }

    private async Task ReadAlarmsAsync()
    {
        try
        {
            using var client = new System.Net.Http.HttpClient { Timeout = TimeSpan.FromSeconds(4) };
            var body = await client.GetStringAsync(_app.Endpoints.ActiveAlarms, _closing.Token)
                .ConfigureAwait(true);
            if (AlarmSummaryReader.TryRead(body, out var counts, out _))
            {
                _counts = counts;
            }
        }
        catch (Exception ex) when (ex is not OperationCanceledException)
        {
            // Leave the previous counts in place; the link status already says
            // the data is not current, and zeroing them here would render as
            // "no alarms" on the tray.
        }
    }

    // ------------------------------------------------------------ commands --

    private void OnReload(object sender, RoutedEventArgs e)
    {
        if (_consoleShown)
        {
            ConsoleView.Reload();
        }
    }

    private void OnOpenAnnunciator(object sender, RoutedEventArgs e) => _app.ShowAnnunciator();

    private void OnOpenInBrowser(object sender, RoutedEventArgs e) =>
        PlatformController.OpenInBrowser(_app.Endpoints.Console);

    private void OnOpenLauncher(object sender, RoutedEventArgs e) => _app.ShowLauncher();

    private void OnOpenSettings(object sender, RoutedEventArgs e) => _app.ShowSettings();

    private void OnRetry(object sender, RoutedEventArgs e)
    {
        RetryButton.Visibility = Visibility.Collapsed;
        StartScreenFromGateButton.Visibility = Visibility.Collapsed;
        LogsButton.Visibility = Visibility.Collapsed;
        GateProgress.Visibility = Visibility.Visible;
        GateTitle.Text = "Starting Project CHAOS";
        GateSummary.Text = "Waiting for the platform gateway to answer.";
        GateFacts.ItemsSource = null;
        GateSteps.ItemsSource = null;
        _ = RunStartupAsync();
    }

    private void OnOpenLogs(object sender, RoutedEventArgs e) => PlatformController.OpenLogFolder();

    private void OnClosed(object sender, WindowEventArgs args)
    {
        _closing.Cancel();
        _app.SaveLayout();
    }
}
