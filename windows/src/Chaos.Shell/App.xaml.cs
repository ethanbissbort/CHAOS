using System.Net.Http;
using Chaos.Shell.Core;
using Microsoft.UI.Dispatching;
using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Media;
using Microsoft.Windows.AppLifecycle;

// Colour types are fully qualified below rather than imported. Both
// Microsoft.UI and Windows.UI expose a "Colors" class, and importing either
// namespace here makes an unqualified "Colors" ambiguous — a compile error the
// owner would meet rather than a CI runner.
namespace Chaos.Shell;

/// <summary>
/// Application root. Loads the operator's settings, resolves where the gateway
/// is, opens the launcher, and owns the tray icon, the platform controller and
/// the annunciator window for the process lifetime.
/// </summary>
/// <remarks>
/// The launcher opens first, always. It is the screen that says what was found
/// and what can be done about it, and it advances to the console by itself once
/// everything is verified — so double-clicking the executable is the only thing
/// an operator has to do, and it is also the only place from which anything
/// gets started.
/// </remarks>
public partial class App : Application
{
    private static readonly HttpClient Http = new() { Timeout = TimeSpan.FromSeconds(5) };

    private readonly ShellStartupOptions _options;
    private readonly AppInstance _instance;
    private readonly DispatcherQueue _dispatcher;
    private readonly List<string> _notices = new();

    private MainWindow? _main;
    private AnnunciatorWindow? _annunciator;
    private LauncherWindow? _launcher;
    private SettingsWindow? _settings;
    private HelpWindow? _help;
    private TrayIcon? _tray;
    private ILayoutStore _layoutStore = null!;
    private ShellLayout _layout = ShellLayout.Empty;
    private DateTimeOffset? _launcherLastShownUtc;
    private bool _launcherDismissed;

    /// <summary>
    /// Startup state handed over by <see cref="ShellEntryPoint"/> before
    /// <c>Application.Start</c>. See the parameterless constructor for why this
    /// exists rather than being passed straight in.
    /// </summary>
    internal static ShellStartupOptions? PendingOptions;
    internal static AppInstance? PendingInstance;
    internal static string[]? PendingArguments;

    /// <summary>
    /// Parameterless constructor, required because the XAML compiler emits its
    /// own <c>Program.Main</c> into App.g.i.cs that calls <c>new App()</c>.
    ///
    /// DISABLE_XAML_GENERATED_MAIN is set in the csproj and StartupObject names
    /// <see cref="ShellEntryPoint"/>, so that generated Main is not the entry
    /// point — but it is still COMPILED, and it will not compile against a
    /// constructor that requires arguments. Rather than keep chasing why the
    /// constant is ignored, this makes the generated code valid.
    ///
    /// It reads the same state the real entry point sets, so the shell behaves
    /// identically even in the case where the generated Main did run. Falling
    /// back to defaults would silently start against the wrong gateway.
    /// </summary>
    public App()
        : this(PendingOptions ?? ShellStartupOptions.Default,
               PendingInstance ?? AppInstance.GetCurrent())
    {
    }

    public App(ShellStartupOptions options, AppInstance instance)
    {
        _options = options;
        _instance = instance;
        _dispatcher = DispatcherQueue.GetForCurrentThread();
        InitializeComponent();
    }

    public HostEndpoints Endpoints { get; private set; } = HostEndpoints.Default;

    /// <summary>Where the gateway was found, and whether a fallback was used.</summary>
    public HostResolution Resolution { get; private set; } = null!;

    /// <summary>The operator's settings, as last saved.</summary>
    public ShellSettings Settings { get; private set; } = ShellSettings.Defaults;

    internal IShellSettingsStore SettingsStore { get; private set; } = null!;

    /// <summary>Starts, stops and watches the platform. Windows-only.</summary>
    internal PlatformController Platform { get; private set; } = null!;

    /// <summary>The shell's HTTP conversation with the gateway.</summary>
    internal PlatformProbeClient Probe { get; } = new(Http);

    /// <summary>Whether this shell has administrator rights. Read once at start.</summary>
    internal bool IsElevated { get; private set; }

    /// <summary>Whether the configured gateway address names this machine.</summary>
    internal bool AddressIsThisMachine { get; private set; } = true;

    /// <summary>
    /// Who owns the running platform, as last established by the launcher. The
    /// exit path and the tray menu key off this, so it is never guessed: it
    /// starts Unknown and is only ever set from a launcher evaluation.
    /// </summary>
    internal PlatformRunMode RunMode { get; private set; } = PlatformRunMode.Unknown;

    /// <summary>Whether the gateway answered recently. Used to decide whether a
    /// served help copy is worth offering at all.</summary>
    internal bool GatewayReachable { get; private set; }

    /// <summary>The documentation listener's URL from /host/info, when reported.</summary>
    internal Uri? DocumentationUrl { get; private set; }

    /// <summary>Records what the shell's poller last saw about the platform.</summary>
    internal void NotePlatformReachability(bool reachable, Uri? documentationUrl)
    {
        GatewayReachable = reachable;

        // Never forget a URL we were once told: /host/info stops answering when
        // the platform stops, and that is precisely when help is wanted.
        if (documentationUrl is not null)
        {
            DocumentationUrl = documentationUrl;
        }
    }

    /// <summary>Things the operator has to be told about this launch.</summary>
    internal IReadOnlyList<string> StartupNotices => _notices;

    /// <summary>The arguments this shell was launched with, for an elevated restart.</summary>
    internal IReadOnlyList<string> LaunchArguments { get; private set; } = Array.Empty<string>();

    internal ShellLayout Layout => _layout;

    protected override void OnLaunched(LaunchActivatedEventArgs args)
    {
        LaunchArguments = PendingArguments ?? Array.Empty<string>();

        SettingsStore = FileShellSettingsStore.Default();
        var load = SettingsStore.Load();
        Settings = load.Settings;

        if (load.Problem is not null)
        {
            _notices.Add(load.Problem);
        }

        Resolution = HostUrlResolver.Resolve(
            _options.HostOverride,
            Environment.GetEnvironmentVariable(HostUrlResolver.EnvironmentVariable),
            Settings);
        Endpoints = Resolution.Endpoints;

        if (Resolution.Problem is not null)
        {
            _notices.Add(Resolution.Problem);
        }

        // A --host or CHAOS_HOST_URL override silently beating the Settings page
        // is exactly the kind of thing that wastes an afternoon, so it is stated.
        if (Resolution.Source is HostUrlSource.CommandLine or HostUrlSource.Environment)
        {
            _notices.Add(HostUrlResolver.DescribeSource(Resolution));
        }

        foreach (var problem in _options.Problems)
        {
            _notices.Add(problem);
        }

        IsElevated = PlatformController.IsElevated();
        AddressIsThisMachine = PlatformController.AddressIsThisMachine(Endpoints.BaseUri.Host);

        Platform = new PlatformController(new PlatformLogBuffer());
        Platform.ChildExited += OnManagedChildExited;

        _layoutStore = FileLayoutStore.Default();
        _layout = _layoutStore.Load();

        ApplyTheme();

        _instance.Activated += OnRedirectedActivation;

        _tray = new TrayIcon(this);
        _tray.Show();

        ShowLauncher();
    }

    /// <summary>A second launch arrived; act on it instead of starting again.</summary>
    private void OnRedirectedActivation(object? sender, AppActivationArguments e)
    {
        _dispatcher.TryEnqueue(() =>
        {
            var payload = ActivationPayload.ParseOrFocusConsole(Environment.CommandLine);

            if (payload.Target == ActivationTarget.Annunciator)
            {
                ShowAnnunciator();
            }
            else
            {
                ShowConsole();
            }
        });
    }

    // ------------------------------------------------------------ windows --

    /// <summary>
    /// Opens the launcher, or brings the existing one forward.
    /// </summary>
    internal void ShowLauncher()
    {
        if (_launcher is null)
        {
            // The local is captured, not the field: by the time the handler
            // runs the field may already hold a newer launcher, and reading it
            // would record the wrong window's dismissal.
            var launcher = new LauncherWindow(this);
            launcher.Closed += (_, _) =>
            {
                _launcherDismissed = launcher.Dismissed;
                if (ReferenceEquals(_launcher, launcher))
                {
                    _launcher = null;
                }
            };

            _launcher = launcher;
            launcher.Activate();
        }
        else
        {
            _launcher.BringToFront();
        }

        _launcherLastShownUtc = DateTimeOffset.UtcNow;
    }

    /// <summary>
    /// Called by the console's poller when it has lost the platform. The
    /// decision to interrupt is <see cref="LauncherReentry"/>'s, not this
    /// window's: one dropped poll must not shove a screen in front of somebody
    /// who is working.
    /// </summary>
    internal void NoteLinkLost(int consecutiveFailures)
    {
        if (_launcher is not null)
        {
            return;
        }

        var since = _launcherLastShownUtc is { } shown ? DateTimeOffset.UtcNow - shown : (TimeSpan?)null;

        if (LauncherReentry.ShouldReturn(consecutiveFailures, since, _launcherDismissed))
        {
            ShowLauncher();
        }
    }

    /// <summary>A successful poll clears the operator's dismissal.</summary>
    internal void NoteLinkRestored() => _launcherDismissed = false;

    /// <summary>Records what the launcher established about who owns the platform.</summary>
    internal void NoteRunMode(PlatformRunMode mode)
    {
        RunMode = mode;
        _tray?.RefreshMenu();
    }

    internal void ShowConsole()
    {
        // A launch asking for the annunciator gets the annunciator; the console
        // is the default, not the only answer.
        if (_options.Activation.Target != ActivationTarget.Console && _annunciator is null)
        {
            ShowAnnunciator();
        }

        if (_main is null)
        {
            _main = new MainWindow(this);
            _main.Closed += (_, _) => _main = null;
            _main.Activate();
            return;
        }

        _main.Activate();
        _main.BringToFront();
    }

    internal void ShowAnnunciator()
    {
        _annunciator ??= CreateAnnunciator();
        _annunciator.Activate();
        _annunciator.BringToFront();
        SaveLayout(annunciatorOpen: true);
    }

    /// <summary>
    /// Opens the manual. One window, reused: F1 from anywhere brings the same
    /// one forward rather than stacking copies.
    /// </summary>
    internal void ShowHelp(string? page = null)
    {
        if (_help is null)
        {
            _help = new HelpWindow(this);
            _help.Closed += (_, _) => _help = null;
            _help.Activate();
        }
        else
        {
            _help.Activate();
            _help.BringToFront();
        }

        if (!string.IsNullOrWhiteSpace(page))
        {
            _help.Navigate(page!);
        }
    }

    internal void ShowSettings()
    {
        if (_settings is null)
        {
            _settings = new SettingsWindow(this);
            _settings.Closed += (_, _) => _settings = null;
            _settings.Activate();
            return;
        }

        _settings.Activate();
    }

    private AnnunciatorWindow CreateAnnunciator()
    {
        var window = new AnnunciatorWindow(this);
        window.Closed += (_, _) =>
        {
            _annunciator = null;
            SaveLayout(annunciatorOpen: false);
        };
        return window;
    }

    // ----------------------------------------------------------- settings --

    /// <summary>
    /// Saves settings and applies everything that can be applied now.
    /// </summary>
    /// <returns>The failure reason, or null on success.</returns>
    internal string? SaveSettings(ShellSettings settings)
    {
        ArgumentNullException.ThrowIfNull(settings);

        var problem = SettingsStore.Save(settings);
        if (problem is not null)
        {
            return problem;
        }

        var previous = Settings;
        Settings = settings;

        if (previous.StartWithWindows != settings.StartWithWindows)
        {
            var registrationProblem = PlatformController.ApplyStartupRegistration(settings.StartWithWindows);
            if (registrationProblem is not null)
            {
                _notices.Add(registrationProblem);
            }
        }

        ApplyTheme();

        // The address only moves when nothing on the command line or in the
        // environment is overriding it — otherwise the shell would silently
        // ignore the override the operator was just told about.
        if (Resolution.Source is HostUrlSource.Settings or HostUrlSource.Default)
        {
            Resolution = HostUrlResolver.Resolve(null, null, settings);
            Endpoints = Resolution.Endpoints;
            AddressIsThisMachine = PlatformController.AddressIsThisMachine(Endpoints.BaseUri.Host);
        }

        return null;
    }

    /// <summary>
    /// Applies the theme by rewriting the shell's brushes in place, so every
    /// <c>StaticResource</c> reference in the project follows without rebinding.
    /// </summary>
    internal void ApplyTheme()
    {
        var dark = Settings.Theme switch
        {
            ShellTheme.Dark => true,
            ShellTheme.Light => false,
            _ => RequestedTheme == ApplicationTheme.Dark,
        };

        SetBrush("ChaosShellBackground", dark ? "#0B0D12" : "#F6F7FA");
        SetBrush("ChaosShellText", dark ? "#E6E9F0" : "#12151C");
        SetBrush("ChaosShellMuted", dark ? "#AAB2C5" : "#4A5265");
        SetBrush("ChaosShellStroke", dark ? "#232936" : "#D6DAE4");
        SetBrush("ChaosShellAccent", dark ? "#5B8CFF" : "#2A5BD7");
        SetBrush("ChaosShellWarn", dark ? "#F5C144" : "#9A6B00");
        SetBrush("ChaosShellError", dark ? "#E5484D" : "#C0272C");
        SetBrush("ChaosShellOk", dark ? "#3DD68C" : "#1B7F4F");

        var element = dark ? ElementTheme.Dark : ElementTheme.Light;
        foreach (var window in new Window?[] { _main, _annunciator, _launcher, _settings })
        {
            if (window?.Content is FrameworkElement root)
            {
                root.RequestedTheme = element;
            }
        }
    }

    private static void SetBrush(string key, string hex)
    {
        if (Current.Resources.TryGetValue(key, out var value) && value is SolidColorBrush brush)
        {
            brush.Color = Parse(hex);
        }
    }

    private static Windows.UI.Color Parse(string hex)
    {
        var span = hex.AsSpan(1);
        return Windows.UI.Color.FromArgb(
            255,
            byte.Parse(span[..2], System.Globalization.NumberStyles.HexNumber, null),
            byte.Parse(span[2..4], System.Globalization.NumberStyles.HexNumber, null),
            byte.Parse(span[4..6], System.Globalization.NumberStyles.HexNumber, null));
    }

    /// <summary>One of the shell's brushes, or grey if the key has gone.</summary>
    internal static SolidColorBrush ShellBrush(string key) =>
        Current.Resources.TryGetValue(key, out var value) && value is SolidColorBrush brush
            ? brush
            : new SolidColorBrush(Microsoft.UI.Colors.Gray);

    // ---------------------------------------------------------- lifecycle --

    /// <summary>
    /// Stops and starts the platform, for a setting that needs it.
    /// </summary>
    /// <returns>What happened, for the operator.</returns>
    internal async Task<string> RestartPlatformAsync()
    {
        using var cancellation = new CancellationTokenSource(TimeSpan.FromMinutes(3));

        if (Platform.ManagedChildRunning)
        {
            Platform.StopManagedChild();
        }
        else if (RunMode == PlatformRunMode.WindowsService)
        {
            var stop = await Platform
                .StopServiceAsync(Settings.ServiceName, cancellation.Token)
                .ConfigureAwait(true);

            if (!stop.Worked)
            {
                return stop.Message;
            }
        }
        else
        {
            return "This shell did not start the platform that is running, so it cannot restart it. "
                + "Restart it the way it was started.";
        }

        var facts = new LauncherFacts
        {
            Endpoints = Endpoints,
            Settings = Settings,
            Service = PlatformController.ProbeService(Settings.ServiceName),
            Executable = PlatformController.LocateExecutable(Settings.HostExecutablePath),
            IsElevated = IsElevated,
            AddressIsThisMachine = AddressIsThisMachine,
        };

        var plan = PlatformStartPlanner.Plan(facts);
        if (!plan.CanStart)
        {
            return $"The platform was stopped, but this shell cannot start it again: {plan.Refusal}";
        }

        if (plan.Method == StartMethod.WindowsService)
        {
            var start = await Platform
                .StartServiceAsync(plan.ServiceName!, cancellation.Token)
                .ConfigureAwait(true);
            return start.Message;
        }

        return Platform.StartManagedChild(plan)
            ?? "The platform was restarted by this shell. IT WILL STOP WHEN YOU CLOSE THIS SHELL.";
    }

    private void OnManagedChildExited(object? sender, string message)
    {
        // The platform this shell was responsible for has died. Say so where
        // the operator is looking, not only in a log.
        _dispatcher.TryEnqueue(() =>
        {
            RunMode = PlatformRunMode.Unknown;
            _tray?.RefreshMenu();
            _notices.Add(message);
            ShowLauncher();
        });
    }

    internal void SaveLayout(bool? annunciatorOpen = null)
    {
        // Saving runs from window Closed handlers, where the AppWindow behind a
        // window may already be gone. Losing a remembered position is a
        // nuisance; taking the shell down on the way out — and with it a
        // platform this shell started — is not.
        try
        {
            _layout = _layout with
            {
                Main = _main?.CurrentPlacement() ?? _layout.Main,
                Annunciator = _annunciator?.CurrentPlacement() ?? _layout.Annunciator,
                AnnunciatorWasOpen = annunciatorOpen ?? _layout.AnnunciatorWasOpen,
                LastHost = Endpoints.BaseUri.ToString(),
            };
        }
        catch (Exception ex) when (ex is InvalidOperationException or System.Runtime.InteropServices.COMException)
        {
            // Keep whatever was last known good.
        }

        _layoutStore.Save(_layout);
    }

    /// <summary>
    /// Publishes the latest link and alarm state to the tray. Called by the
    /// console window's poller; the tray never polls on its own so both cannot
    /// disagree about what the platform last said.
    /// </summary>
    internal void UpdateTray(LinkStatus link, AlarmCounts counts)
    {
        var state = TrayStateFactory.Create(link, counts, DateTimeOffset.UtcNow);
        _dispatcher.TryEnqueue(() => _tray?.Apply(state));
    }

    /// <summary>
    /// Exits.
    /// </summary>
    /// <remarks>
    /// What this does to the platform depends entirely on how the platform is
    /// running, so the confirmation is written from the run mode rather than
    /// being a fixed sentence. A platform started by this shell DIES HERE, and
    /// that case gets a different title, different wording and a non-default
    /// confirm button.
    /// </remarks>
    internal async void ExitShell(bool skipConfirmation = false)
    {
        if (!skipConfirmation && !await ConfirmExitAsync().ConfigureAwait(true))
        {
            return;
        }

        SaveLayout();
        _tray?.Dispose();
        _tray = null;

        // Disposing the controller stops a managed child. That is the whole
        // point of having warned first.
        Platform?.Dispose();

        Exit();
    }

    private async Task<bool> ConfirmExitAsync()
    {
        var stopsThePlatform = RunModeBanner.For(RunMode, Settings.ServiceName)
            .ClosingTheShellStopsThePlatform;

        // Assigned in steps rather than chained: ?? is right-associative, so
        // "a ?? b ?? c ?? d" reduces to (c ?? d) first, and two unrelated window
        // types have no common type to coalesce to.
        Window? host = _launcher;
        host ??= _main;
        host ??= _annunciator;
        host ??= _settings;
        if (host?.Content is not FrameworkElement root)
        {
            // Without somewhere to show a dialog, the safe answer is to refuse
            // the silent stop and keep running.
            return !stopsThePlatform;
        }

        var dialog = new Microsoft.UI.Xaml.Controls.ContentDialog
        {
            XamlRoot = root.XamlRoot,
            Title = stopsThePlatform
                ? ShellMessages.ExitStopsPlatformTitle
                : ShellMessages.ExitConfirmationTitle,
            Content = new Microsoft.UI.Xaml.Controls.TextBlock
            {
                Text = ShellMessages.ExitConfirmationFor(RunMode, Settings.ServiceName),
                TextWrapping = TextWrapping.Wrap,
            },
            PrimaryButtonText = stopsThePlatform ? "Exit and stop the platform" : "Exit shell",
            CloseButtonText = "Stay open",

            // Exiting is never the default when it would stop the platform.
            DefaultButton = stopsThePlatform
                ? Microsoft.UI.Xaml.Controls.ContentDialogButton.Close
                : Microsoft.UI.Xaml.Controls.ContentDialogButton.Primary,
        };

        return await dialog.ShowAsync() == Microsoft.UI.Xaml.Controls.ContentDialogResult.Primary;
    }
}
