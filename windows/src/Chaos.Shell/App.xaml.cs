using Chaos.Shell.Core;
using Microsoft.UI.Dispatching;
using Microsoft.UI.Xaml;
using Microsoft.Windows.AppLifecycle;

namespace Chaos.Shell;

/// <summary>
/// Application root. Resolves where the gateway is, opens the console window,
/// and owns the tray icon and the annunciator window for the process lifetime.
/// </summary>
public partial class App : Application
{
    private readonly ShellStartupOptions _options;
    private readonly AppInstance _instance;
    private readonly DispatcherQueue _dispatcher;

    private MainWindow? _main;
    private AnnunciatorWindow? _annunciator;
    private TrayIcon? _tray;
    private ILayoutStore _layoutStore = null!;
    private ShellLayout _layout = ShellLayout.Empty;

    /// <summary>
    /// Startup state handed over by <see cref="ShellEntryPoint"/> before
    /// <c>Application.Start</c>. See the parameterless constructor for why this
    /// exists rather than being passed straight in.
    /// </summary>
    internal static ShellStartupOptions? PendingOptions;
    internal static AppInstance? PendingInstance;

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

    internal ShellLayout Layout => _layout;

    protected override void OnLaunched(LaunchActivatedEventArgs args)
    {
        Resolution = HostUrlResolver.Resolve(
            _options.HostOverride,
            Environment.GetEnvironmentVariable(HostUrlResolver.EnvironmentVariable));
        Endpoints = Resolution.Endpoints;

        _layoutStore = FileLayoutStore.Default();
        _layout = _layoutStore.Load();

        _instance.Activated += OnRedirectedActivation;

        _tray = new TrayIcon(this);
        _tray.Show();

        _main = new MainWindow(this);
        _main.Activate();

        if (_options.Activation.Target == ActivationTarget.Annunciator || _layout.AnnunciatorWasOpen)
        {
            ShowAnnunciator();
        }
    }

    /// <summary>A second launch arrived; act on it instead of starting again.</summary>
    private void OnRedirectedActivation(object? sender, AppActivationArguments e)
    {
        _dispatcher.TryEnqueue(() =>
        {
            var payload = ActivationPayload.ParseOrFocusConsole(
                Environment.CommandLine);

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

    internal void ShowConsole()
    {
        if (_main is null)
        {
            _main = new MainWindow(this);
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

    internal void SaveLayout(bool? annunciatorOpen = null)
    {
        _layout = _layout with
        {
            Main = _main?.CurrentPlacement() ?? _layout.Main,
            Annunciator = _annunciator?.CurrentPlacement() ?? _layout.Annunciator,
            AnnunciatorWasOpen = annunciatorOpen ?? _layout.AnnunciatorWasOpen,
            LastHost = Endpoints.BaseUri.ToString(),
        };
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
    /// Closing the last window must NOT stop the platform: the Windows service
    /// keeps running. Exiting is therefore explicit, and the tray says so.
    /// </summary>
    internal void ExitShell()
    {
        SaveLayout();
        _tray?.Dispose();
        _tray = null;
        Exit();
    }
}
