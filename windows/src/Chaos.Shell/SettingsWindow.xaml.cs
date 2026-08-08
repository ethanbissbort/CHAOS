using Chaos.Shell.Core;
using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Controls;
using Windows.Graphics;

namespace Chaos.Shell;

/// <summary>
/// The settings page, native and persisted.
///
/// Reading, writing, validating and working out what a change will actually do
/// all live in Chaos.Shell.Core and are tested there. This window fills boxes
/// from a <see cref="ShellSettings"/>, reads them back into one, and shows what
/// the validator and <see cref="SettingsChangeImpact"/> say about it.
/// </summary>
public sealed partial class SettingsWindow : Window
{
    private readonly App _app;
    private readonly List<MonitorInfo> _monitors = new();

    private ShellSettings _original;

    public SettingsWindow(App app)
    {
        _app = app;
        _original = app.Settings;

        InitializeComponent();

        Title = "Project CHAOS — Settings";
        AppWindow.Resize(new SizeInt32(900, 940));

        ThemeBox.Items.Add("Follow Windows");
        ThemeBox.Items.Add("Always dark");
        ThemeBox.Items.Add("Always light");

        LoadMonitors();
        Load(_original);

        StorePathText.Text = $"Saved in {app.SettingsStore.Path}";
    }

    private void LoadMonitors()
    {
        _monitors.Clear();
        _monitors.AddRange(MonitorEnumerator.Current());

        MonitorBox.Items.Add("Primary display");
        foreach (var monitor in _monitors)
        {
            var size = monitor.WorkArea;
            MonitorBox.Items.Add(
                $"Display {monitor.Index}{(monitor.IsPrimary ? " (primary)" : string.Empty)} — "
                + $"{size.Width}×{size.Height} — {monitor.DeviceId}");
        }
    }

    // --------------------------------------------------------------- load --

    private void Load(ShellSettings settings)
    {
        AddressBox.Text = settings.HostAddress;
        PortBox.Value = settings.HostPort;
        HttpsSwitch.IsOn = settings.UseHttps;

        AutoStartSwitch.IsOn = settings.AutoStartPlatform;
        StartWithWindowsSwitch.IsOn = settings.StartWithWindows;
        PreferServiceSwitch.IsOn = settings.PreferWindowsService;
        ServiceNameBox.Text = settings.ServiceName;
        ExecutableBox.Text = settings.HostExecutablePath ?? string.Empty;

        DatabaseBox.Text = settings.DatabasePath ?? string.Empty;
        DataDirectoryBox.Text = settings.DataDirectory ?? string.Empty;

        ThemeBox.SelectedIndex = settings.Theme switch
        {
            ShellTheme.Dark => 1,
            ShellTheme.Light => 2,
            _ => 0,
        };

        MonitorBox.SelectedIndex = MonitorIndexFor(settings.AnnunciatorMonitor);
        FullScreenSwitch.IsOn = settings.AnnunciatorFullScreen;

        // These two notes state the limits of what this page can do, so an
        // operator is never left believing a setting applies where it does not.
        PreferServiceNote.Text =
            "A platform started by this shell STOPS when the shell closes. A Windows service does not.";

        StorageScopeNote.Text =
            "These are applied to a platform this shell starts itself. A Windows service reads its "
            + "own configuration, installed once, which this shell does not change — so on a node "
            + "running the service these boxes will not move anything.";

        var probe = PlatformController.LocateExecutable(settings.HostExecutablePath);
        ExecutableFound.Text = HostExecutableLocator.Describe(probe);
    }

    private int MonitorIndexFor(string? request)
    {
        if (string.IsNullOrWhiteSpace(request)
            || string.Equals(request, "primary", StringComparison.OrdinalIgnoreCase))
        {
            return 0;
        }

        for (var i = 0; i < _monitors.Count; i++)
        {
            if (string.Equals(_monitors[i].DeviceId, request, StringComparison.OrdinalIgnoreCase)
                || _monitors[i].Index.ToString(System.Globalization.CultureInfo.InvariantCulture) == request)
            {
                return i + 1;
            }
        }

        // The saved display is not attached now. Fall back to primary rather
        // than to a display that is not there — the resolver would relocate the
        // panel anyway, and showing "Primary" is the honest reading.
        return 0;
    }

    // --------------------------------------------------------------- read --

    private ShellSettings Read()
    {
        var monitor = MonitorBox.SelectedIndex > 0 && MonitorBox.SelectedIndex - 1 < _monitors.Count
            ? _monitors[MonitorBox.SelectedIndex - 1].DeviceId
            : null;

        // NumberBox hands back NaN for an empty box. Keeping the previous value
        // rather than coercing to zero means the validator can report a real
        // port problem instead of a fabricated one.
        var port = double.IsNaN(PortBox.Value)
            ? _original.HostPort
            : (int)Math.Round(PortBox.Value);

        return _original with
        {
            HostAddress = AddressBox.Text,
            HostPort = port,
            UseHttps = HttpsSwitch.IsOn,
            AutoStartPlatform = AutoStartSwitch.IsOn,
            StartWithWindows = StartWithWindowsSwitch.IsOn,
            PreferWindowsService = PreferServiceSwitch.IsOn,
            ServiceName = ServiceNameBox.Text,
            HostExecutablePath = Blank(ExecutableBox.Text),
            DatabasePath = Blank(DatabaseBox.Text),
            DataDirectory = Blank(DataDirectoryBox.Text),
            Theme = ThemeBox.SelectedIndex switch
            {
                1 => ShellTheme.Dark,
                2 => ShellTheme.Light,
                _ => ShellTheme.System,
            },
            AnnunciatorMonitor = monitor,
            AnnunciatorFullScreen = FullScreenSwitch.IsOn,
        };
    }

    private static string? Blank(string? value) =>
        string.IsNullOrWhiteSpace(value) ? null : value.Trim();

    // --------------------------------------------------------------- save --

    private async void OnSave(object sender, RoutedEventArgs e)
    {
        // Nothing may escape an async void handler: an unhandled exception ends
        // the process, and ending the process stops a platform this shell
        // started.
        try
        {
            await SaveAsync().ConfigureAwait(true);
        }
        catch (Exception ex)
        {
            StatusText.Text = $"The settings could not be saved: {ex.Message}";
        }
    }

    private async Task SaveAsync()
    {
        var validation = ShellSettingsValidator.Validate(Read());
        ShowProblems(validation);

        if (!validation.IsValid)
        {
            StatusText.Text =
                "Nothing was saved. Correct the settings marked above — your existing settings are "
                + "untouched.";
            return;
        }

        var settings = validation.Normalised;
        var impact = SettingsChangeImpact.Evaluate(_original, settings, _app.RunMode);

        if (!impact.AnythingChanged)
        {
            StatusText.Text = "Nothing changed.";
            return;
        }

        if (!await ConfirmAsync(impact).ConfigureAwait(true))
        {
            return;
        }

        var problem = _app.SaveSettings(settings);
        if (problem is not null)
        {
            StatusText.Text = $"The settings could not be saved: {problem}";
            return;
        }

        _original = settings;
        StatusText.Text = impact.Headline;

        if (impact.RequiresPlatformRestart)
        {
            await OfferRestartAsync().ConfigureAwait(true);
        }
    }

    private void ShowProblems(SettingsValidation validation)
    {
        Show(AddressProblem, validation.For(SettingsField.HostAddress));
        Show(PortProblem, validation.For(SettingsField.HostPort));
        Show(ServiceNameProblem, validation.For(SettingsField.ServiceName));
        Show(ExecutableProblem, validation.For(SettingsField.HostExecutablePath));
        Show(DatabaseProblem, validation.For(SettingsField.DatabasePath));
        Show(DataDirectoryProblem, validation.For(SettingsField.DataDirectory));
    }

    private static void Show(TextBlock target, string? message)
    {
        target.Text = message ?? string.Empty;
        target.Visibility = message is null ? Visibility.Collapsed : Visibility.Visible;
    }

    private async Task<bool> ConfirmAsync(SettingsImpact impact)
    {
        if (Content is not FrameworkElement root)
        {
            return false;
        }

        var body = new StackPanel { Spacing = 8 };

        foreach (var note in impact.Notes)
        {
            body.Children.Add(new TextBlock { Text = note, TextWrapping = TextWrapping.Wrap });
        }

        // Warnings say what will NOT happen. They come last and they are
        // coloured, because a setting that looks saved but is ignored is worse
        // than one that was refused.
        foreach (var warning in impact.Warnings)
        {
            body.Children.Add(new TextBlock
            {
                Text = warning,
                TextWrapping = TextWrapping.Wrap,
                Foreground = App.ShellBrush("ChaosShellWarn"),
            });
        }

        var dialog = new ContentDialog
        {
            XamlRoot = root.XamlRoot,
            Title = impact.Headline,
            Content = new ScrollViewer { Content = body, MaxHeight = 420 },
            PrimaryButtonText = "Save",
            CloseButtonText = "Cancel",
            DefaultButton = ContentDialogButton.Primary,
        };

        return await dialog.ShowAsync() == ContentDialogResult.Primary;
    }

    private async Task OfferRestartAsync()
    {
        if (Content is not FrameworkElement root)
        {
            return;
        }

        var dialog = new ContentDialog
        {
            XamlRoot = root.XamlRoot,
            Title = "Restart the platform now?",
            Content = new TextBlock
            {
                Text =
                    "The change is saved but will not take effect until the platform restarts. "
                    + "Restarting stops it for a few seconds: nothing is controlled and no alarms are "
                    + "evaluated during that time.",
                TextWrapping = TextWrapping.Wrap,
            },
            PrimaryButtonText = "Restart the platform",
            CloseButtonText = "Later",

            // Not the default. Nothing that interrupts control happens on a
            // stray Enter key.
            DefaultButton = ContentDialogButton.Close,
        };

        if (await dialog.ShowAsync() != ContentDialogResult.Primary)
        {
            StatusText.Text =
                "Saved. The platform is still running with its previous settings until it is restarted.";
            return;
        }

        StatusText.Text = await _app.RestartPlatformAsync().ConfigureAwait(true);
    }

    private void OnCancel(object sender, RoutedEventArgs e) => Close();

    private void OnReset(object sender, RoutedEventArgs e)
    {
        // Only the boxes are reset. Nothing is written until Save, so this is
        // recoverable with Cancel.
        Load(ShellSettings.Defaults with { ServiceName = ShellMessages.ServiceName });
        StatusText.Text = "The boxes are back at their defaults. Nothing is saved until you press Save.";
    }

    private void OnHelpRequested(
        Microsoft.UI.Xaml.Input.KeyboardAccelerator sender,
        Microsoft.UI.Xaml.Input.KeyboardAcceleratorInvokedEventArgs args)
    {
        args.Handled = true;
        _app.ShowHelp("desktop-shell.html");
    }
}
