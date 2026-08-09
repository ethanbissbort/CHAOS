using Chaos.Shell.Core;
using Microsoft.UI.Xaml;
using Microsoft.UI.Windowing;
using Windows.Graphics;

namespace Chaos.Shell;

/// <summary>
/// The annunciator panel in its own window.
///
/// It is a separate window rather than a tab because that is how it is used:
/// on a wall display, often on a different monitor from the console, sometimes
/// full-screen for a whole shift. Keyboard: F11 full-screen, F10 always-on-top,
/// Escape leaves full-screen.
/// </summary>
public sealed partial class AnnunciatorWindow : Window
{
    private readonly App _app;
    private bool _fullScreen;
    private bool _alwaysOnTop;

    public AnnunciatorWindow(App app)
    {
        _app = app;
        InitializeComponent();

        Title = "Project CHAOS — Annunciator";

        RestorePlacement();

        Panel.NavigationCompleted += (_, _) =>
        {
            LoadingPanel.Visibility = Visibility.Collapsed;
        };

        try
        {
            Panel.Source = app.Endpoints.Annunciator;
        }
        catch (Exception ex)
        {
            LoadingText.Text = $"Panel unavailable: {ex.Message}";
        }

        Closed += (_, _) => _app.SaveLayout(annunciatorOpen: false);
    }

    private void RestorePlacement()
    {
        var saved = _app.Layout.Annunciator;
        var settings = _app.Settings;
        var monitors = MonitorEnumerator.Current();

        if (monitors.Count == 0)
        {
            AppWindow.Resize(new SizeInt32(1440, 920));
            return;
        }

        if (!string.IsNullOrWhiteSpace(settings.AnnunciatorMonitor))
        {
            // An explicit display in Settings beats wherever the panel happened
            // to be dragged last time. Select falls back to the primary when
            // the chosen display is not attached, so a wall panel that has been
            // unplugged still opens somewhere an operator can see it.
            var target = WindowPlacementResolver.Select(monitors, settings.AnnunciatorMonitor);
            var area = target.WorkArea;
            AppWindow.MoveAndResize(new RectInt32(area.Left, area.Top, area.Width, area.Height));
        }
        else
        {
            var resolved = WindowPlacementResolver.Resolve(
                saved, monitors, new ScreenRect(0, 0, 1440, 920));
            var b = resolved.Bounds;
            AppWindow.MoveAndResize(new RectInt32(b.Left, b.Top, b.Width, b.Height));
        }

        if (settings.AnnunciatorFullScreen || saved?.FullScreen == true)
        {
            SetFullScreen(true);
        }

        if (saved?.AlwaysOnTop == true)
        {
            SetAlwaysOnTop(true);
        }
    }

    internal WindowPlacement? CurrentPlacement()
    {
        var pos = AppWindow.Position;
        var size = AppWindow.Size;
        var placement = WindowPlacement.FromBounds(
            new ScreenRect(pos.X, pos.Y, size.Width, size.Height));
        return placement with { FullScreen = _fullScreen, AlwaysOnTop = _alwaysOnTop };
    }

    internal void BringToFront()
    {
        AppWindow.Show();
        if (AppWindow.Presenter is OverlappedPresenter p)
        {
            p.Restore();
        }
    }

    internal void SetFullScreen(bool on)
    {
        _fullScreen = on;
        AppWindow.SetPresenter(on ? AppWindowPresenterKind.FullScreen : AppWindowPresenterKind.Overlapped);

        // A wall display showing the alarm panel must not let the machine sleep
        // or blank. Released as soon as full-screen ends.
        DisplayGuard.KeepAwake(on);
    }

    internal void SetAlwaysOnTop(bool on)
    {
        _alwaysOnTop = on;
        if (AppWindow.Presenter is OverlappedPresenter p)
        {
            p.IsAlwaysOnTop = on;
        }
    }

    private void OnHelpRequested(
        Microsoft.UI.Xaml.Input.KeyboardAccelerator sender,
        Microsoft.UI.Xaml.Input.KeyboardAcceleratorInvokedEventArgs args)
    {
        args.Handled = true;
        _app.ShowHelp("annunciator.html");
    }
}
