using Chaos.Shell.Core;
using Microsoft.UI.Xaml;
using Windows.Graphics;

namespace Chaos.Shell;

/// <summary>
/// The manual, opened with F1 from any window.
///
/// It prefers the copy installed on this machine over anything the platform
/// serves — see <see cref="HelpLocator"/>. Help is most needed when the
/// platform is down, so a help window that depends on the platform being up
/// would be missing at exactly the wrong moment.
/// </summary>
public sealed partial class HelpWindow : Window
{
    private readonly App _app;

    public HelpWindow(App app)
    {
        _app = app;
        InitializeComponent();

        Title = "Project CHAOS — Help";
        AppWindow.Resize(new SizeInt32(1180, 900));

        Load();
    }

    /// <summary>Where this window is currently reading from.</summary>
    internal HelpLocation Location { get; private set; } = null!;

    private void Load()
    {
        Location = HelpLocator.Resolve(
            AppContext.BaseDirectory,
            File.Exists,
            _app.DocumentationUrl,
            _app.Endpoints,
            _app.GatewayReachable);

        SourceText.Text = Location.WorksOffline
            ? $"Offline copy · {Location.Summary}"
            : Location.Summary;

        if (Location.Target is null)
        {
            MissingSummary.Text = Location.Summary;
            MissingSearched.ItemsSource = Location.Searched.ToList();
            MissingPanel.Visibility = Visibility.Visible;
            HelpView.Visibility = Visibility.Collapsed;
            return;
        }

        try
        {
            HelpView.Source = Location.Target;
            HelpView.Visibility = Visibility.Visible;
            MissingPanel.Visibility = Visibility.Collapsed;
        }
        catch (Exception ex)
        {
            // Almost always a missing WebView2 runtime. Say what happened rather
            // than leaving an empty window.
            MissingSummary.Text = $"The help viewer could not start: {ex.Message}";
            MissingSearched.ItemsSource = Location.Searched.ToList();
            MissingPanel.Visibility = Visibility.Visible;
            HelpView.Visibility = Visibility.Collapsed;
        }
    }

    internal void BringToFront()
    {
        AppWindow.Show();
        if (AppWindow.Presenter is Microsoft.UI.Windowing.OverlappedPresenter presenter)
        {
            presenter.Restore();
        }
    }

    /// <summary>Opens a specific page, for a context-sensitive help request.</summary>
    internal void Navigate(string pageFileName)
    {
        if (Location.Target is null || string.IsNullOrWhiteSpace(pageFileName))
        {
            return;
        }

        // The single-file build is one document with in-page anchors, so a page
        // name becomes a fragment there and a sibling file when served.
        var target = Location.Kind == HelpSourceKind.LocalFile
            ? new Uri($"{Location.Target}#{Path.GetFileNameWithoutExtension(pageFileName)}")
            : new Uri(Location.Target, pageFileName);

        HelpView.Source = target;
    }

    private void OnBack(object sender, RoutedEventArgs e)
    {
        if (HelpView.CanGoBack)
        {
            HelpView.GoBack();
        }
    }

    private void OnHome(object sender, RoutedEventArgs e)
    {
        if (Location.Target is not null)
        {
            HelpView.Source = Location.Target;
        }
    }

    private void OnOpenInBrowser(object sender, RoutedEventArgs e)
    {
        if (Location.Target is not null)
        {
            PlatformController.OpenInBrowser(Location.Target);
        }
    }

    private void OnRetry(object sender, RoutedEventArgs e) => Load();
}
