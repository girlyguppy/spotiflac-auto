package main

import (
	"context"
	"encoding/json"
	"flag"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"time"

	"github.com/afkarxyz/SpotiFLAC/backend"
)

var version = "dev"

type Config struct {
	DownloadPath         string `json:"downloadPath"`
	Downloader           string `json:"downloader"`
	AutoOrder            string `json:"autoOrder"`
	AutoQuality          string `json:"autoQuality"`
	TidalQuality         string `json:"tidalQuality"`
	QobuzQuality         string `json:"qobuzQuality"`
	AmazonQuality        string `json:"amazonQuality"`
	FilenamePreset       string `json:"filenamePreset"`
	FilenameTemplate     string `json:"filenameTemplate"`
	FolderPreset         string `json:"folderPreset"`
	FolderTemplate       string `json:"folderTemplate"`
	EmbedLyrics          bool   `json:"embedLyrics"`
	EmbedGenre           bool   `json:"embedGenre"`
	EmbedMaxQualityCover bool   `json:"embedMaxQualityCover"`
	TrackNumber          bool   `json:"trackNumber"`
	AllowFallback        bool   `json:"allowFallback"`
	UseFirstArtistOnly   bool   `json:"useFirstArtistOnly"`
	UseSingleGenre       bool   `json:"useSingleGenre"`
	UseSpotFetchAPI      bool   `json:"useSpotFetchAPI"`
	SpotFetchAPIUrl      string `json:"spotFetchAPIUrl"`
}

func loadConfig(path string) (*Config, error) {
	if path == "" {
		dir, err := backend.GetFFmpegDir()
		if err != nil {
			return nil, err
		}
		path = filepath.Join(dir, "config.json")
	}
	data, err := os.ReadFile(path)
	if err != nil {
		if os.IsNotExist(err) {
			return &Config{
				DownloadPath:  backend.GetDefaultMusicPath(),
				Downloader:    "auto",
				AutoOrder:     "tidal-qobuz-amazon-deezer",
				AutoQuality:   "24",
				TidalQuality:  "LOSSLESS",
				QobuzQuality:  "6",
				AmazonQuality: "original",
				FilenamePreset: "title-artist",
				FolderPreset:   "album-artist-album",
				FolderTemplate: "{album_artist}/{album}",
				AllowFallback:  true,
				EmbedGenre:     true,
			}, nil
		}
		return nil, err
	}
	cfg := &Config{
		DownloadPath:  backend.GetDefaultMusicPath(),
		Downloader:    "auto",
		AutoOrder:     "tidal-qobuz-amazon-deezer",
		AutoQuality:   "24",
		TidalQuality:  "LOSSLESS",
		QobuzQuality:  "6",
		AmazonQuality: "original",
		FilenamePreset: "title-artist",
		FolderPreset:   "album-artist-album",
		FolderTemplate: "{album_artist}/{album}",
		AllowFallback:  true,
		EmbedGenre:     true,
	}
	if err := json.Unmarshal(data, cfg); err != nil {
		return nil, fmt.Errorf("invalid config: %w", err)
	}
	return cfg, nil
}

func filenameFormat(cfg *Config) string {
	if cfg.FilenamePreset != "" && !strings.Contains(cfg.FilenamePreset, "{") {
		return cfg.FilenamePreset
	}
	if cfg.FilenameTemplate != "" {
		return cfg.FilenameTemplate
	}
	return "title-artist"
}

func buildFolderPath(baseDir string, cfg *Config, albumArtist, albumName, releaseDate string) string {
	tmpl := cfg.FolderTemplate
	if tmpl == "" {
		switch cfg.FolderPreset {
		case "album-artist-album":
			tmpl = "{album_artist}/{album}"
		case "artist-album":
			tmpl = "{artist}/{album}"
		case "album":
			tmpl = "{album}"
		default:
			tmpl = "{album_artist}/{album}"
		}
	}

	year := ""
	if len(releaseDate) >= 4 {
		year = releaseDate[:4]
	}

	result := tmpl
	result = strings.ReplaceAll(result, "{album_artist}", backend.SanitizeFilename(albumArtist))
	result = strings.ReplaceAll(result, "{album}", backend.SanitizeFilename(albumName))
	result = strings.ReplaceAll(result, "{year}", year)
	result = strings.ReplaceAll(result, "{date}", backend.SanitizeFilename(releaseDate))

	return filepath.Join(baseDir, result)
}

func audioFormat(cfg *Config, service string) string {
	switch service {
	case "tidal":
		if cfg.TidalQuality != "" {
			return cfg.TidalQuality
		}
		return "LOSSLESS"
	case "qobuz":
		if cfg.QobuzQuality != "" {
			return cfg.QobuzQuality
		}
		return "6"
	case "amazon":
		if cfg.AmazonQuality != "" {
			return cfg.AmazonQuality
		}
		return "original"
	case "deezer":
		return "FLAC"
	default:
		return "LOSSLESS"
	}
}

func serviceOrder(cfg *Config) []string {
	if cfg.Downloader != "" && cfg.Downloader != "auto" {
		return []string{cfg.Downloader}
	}
	if cfg.AutoOrder != "" {
		return strings.Split(cfg.AutoOrder, "-")
	}
	return []string{"tidal", "qobuz", "amazon", "deezer"}
}

func downloadTrack(
	cfg *Config,
	service string,
	spotifyID string,
	trackName, artistName, albumName, albumArtist, releaseDate, coverURL string,
	trackNumber, discNumber, totalTracks, totalDiscs int,
	copyright, publisher string,
	outputDir string,
	position int,
	saveLyrics bool,
	embedLyrics bool,
) (filename string, err error) {
	fnFormat := filenameFormat(cfg)

	spotifyURL := ""
	if spotifyID != "" {
		spotifyURL = fmt.Sprintf("https://open.spotify.com/track/%s", spotifyID)
	}

	// Check if file already exists
	expectedFilename := backend.BuildExpectedFilename(
		trackName, artistName, albumName, albumArtist, releaseDate,
		fnFormat, "", "", cfg.TrackNumber, position, discNumber, true,
	)
	expectedPath := filepath.Join(outputDir, expectedFilename)
	if fileInfo, statErr := os.Stat(expectedPath); statErr == nil && fileInfo.Size() > 100*1024 {
		fmt.Printf("  SKIP (exists): %s\n", expectedFilename)
		return expectedPath, nil
	}

	if err := os.MkdirAll(outputDir, 0755); err != nil {
		return "", fmt.Errorf("failed to create output dir: %w", err)
	}

	// Fetch lyrics in background
	type lyricsResult struct {
		lrc    string
		source string
	}
	lyricsChan := make(chan lyricsResult, 1)
	if (saveLyrics || embedLyrics) && spotifyID != "" {
		go func() {
			client := backend.NewLyricsClient()
			resp, src, err := client.FetchLyricsAllSources(spotifyID, trackName, artistName, 0)
			if err == nil && resp != nil && len(resp.Lines) > 0 {
				lrc := client.ConvertToLRC(resp, trackName, artistName)
				lyricsChan <- lyricsResult{lrc: lrc, source: src}
			} else {
				lyricsChan <- lyricsResult{}
			}
		}()
	} else {
		lyricsChan <- lyricsResult{}
	}

	// Fetch ISRC in background (needed for Qobuz)
	isrcChan := make(chan string, 1)
	if spotifyID != "" {
		go func() {
			client := backend.NewSongLinkClient()
			isrc, _ := client.GetISRC(spotifyID)
			isrcChan <- isrc
		}()
	} else {
		isrcChan <- ""
	}

	// Try download
	switch service {
	case "tidal":
		dl := backend.NewTidalDownloader("")
		filename, err = dl.Download(
			spotifyID, outputDir, audioFormat(cfg, "tidal"), fnFormat,
			cfg.TrackNumber, position,
			trackName, artistName, albumName, albumArtist, releaseDate,
			true, coverURL, cfg.EmbedMaxQualityCover,
			trackNumber, discNumber, totalTracks, totalDiscs,
			copyright, publisher, spotifyURL,
			cfg.AllowFallback, cfg.UseFirstArtistOnly, cfg.UseSingleGenre, cfg.EmbedGenre,
		)
	case "qobuz":
		isrc := <-isrcChan
		dl := backend.NewQobuzDownloader()
		filename, err = dl.DownloadTrackWithISRC(
			isrc, spotifyID, outputDir, audioFormat(cfg, "qobuz"), fnFormat,
			cfg.TrackNumber, position,
			trackName, artistName, albumName, albumArtist, releaseDate,
			true, coverURL, cfg.EmbedMaxQualityCover,
			trackNumber, discNumber, totalTracks, totalDiscs,
			copyright, publisher, spotifyURL,
			cfg.AllowFallback, cfg.UseFirstArtistOnly, cfg.UseSingleGenre, cfg.EmbedGenre,
		)
	case "deezer":
		dl := backend.NewDeezerDownloader()
		filename, err = dl.Download(
			spotifyID, outputDir, fnFormat,
			"", "", cfg.TrackNumber, position,
			trackName, artistName, albumName, albumArtist, releaseDate,
			coverURL,
			trackNumber, discNumber, totalTracks,
			cfg.EmbedMaxQualityCover, totalDiscs,
			copyright, publisher, spotifyURL,
			cfg.UseFirstArtistOnly, cfg.UseSingleGenre, cfg.EmbedGenre,
		)
	case "amazon":
		dl := backend.NewAmazonDownloader()
		filename, err = dl.DownloadBySpotifyID(
			spotifyID, outputDir, audioFormat(cfg, "amazon"), fnFormat,
			"", "", cfg.TrackNumber, position,
			trackName, artistName, albumName, albumArtist, releaseDate,
			coverURL,
			trackNumber, discNumber, totalTracks,
			cfg.EmbedMaxQualityCover, totalDiscs,
			copyright, publisher, spotifyURL,
			cfg.UseFirstArtistOnly, cfg.UseSingleGenre, cfg.EmbedGenre,
		)
	default:
		return "", fmt.Errorf("unknown service: %s", service)
	}

	if err != nil {
		// Clean up partial file
		if filename != "" && !strings.HasPrefix(filename, "EXISTS:") {
			if _, statErr := os.Stat(filename); statErr == nil {
				os.Remove(filename)
			}
		}
		return "", err
	}

	if strings.HasPrefix(filename, "EXISTS:") {
		filename = strings.TrimPrefix(filename, "EXISTS:")
		fmt.Printf("  SKIP (exists): %s\n", filepath.Base(filename))
		return filename, nil
	}

	// Handle lyrics
	lr := <-lyricsChan
	if lr.lrc != "" {
		// Save .lrc file
		if saveLyrics {
			lrcPath := strings.TrimSuffix(filename, filepath.Ext(filename)) + ".lrc"
			if writeErr := os.WriteFile(lrcPath, []byte(lr.lrc), 0644); writeErr != nil {
				fmt.Printf("  WARN: failed to save lyrics: %v\n", writeErr)
			} else {
				fmt.Printf("  Lyrics saved (%s)\n", lr.source)
			}
		}
		// Embed lyrics in audio file
		if embedLyrics {
			ext := strings.ToLower(filepath.Ext(filename))
			if ext == ".flac" || ext == ".mp3" || ext == ".m4a" {
				if embedErr := backend.EmbedLyricsOnlyUniversal(filename, lr.lrc); embedErr != nil {
					fmt.Printf("  WARN: failed to embed lyrics: %v\n", embedErr)
				}
			}
		}
	}

	return filename, nil
}

func downloadWithFallback(
	cfg *Config,
	spotifyID string,
	trackName, artistName, albumName, albumArtist, releaseDate, coverURL string,
	trackNumber, discNumber, totalTracks, totalDiscs int,
	copyright, publisher string,
	outputDir string,
	position int,
	saveLyrics bool,
	embedLyrics bool,
) (string, error) {
	services := serviceOrder(cfg)
	var lastErr error

	for _, svc := range services {
		filename, err := downloadTrack(
			cfg, svc, spotifyID,
			trackName, artistName, albumName, albumArtist, releaseDate, coverURL,
			trackNumber, discNumber, totalTracks, totalDiscs,
			copyright, publisher, outputDir, position, saveLyrics, embedLyrics,
		)
		if err == nil {
			return filename, nil
		}
		lastErr = err
		fmt.Printf("  %s failed: %v\n", svc, err)
	}

	return "", fmt.Errorf("all services failed, last error: %w", lastErr)
}

func main() {
	var (
		outputDir   string
		service     string
		configPath  string
		saveLyrics  bool
		embedLyrics bool
		showVersion bool
		jsonOutput  bool
	)

	flag.StringVar(&outputDir, "output", "", "Output directory (default: from config)")
	flag.StringVar(&outputDir, "o", "", "Output directory (shorthand)")
	flag.StringVar(&service, "service", "", "Download service: tidal/qobuz/deezer/amazon/auto (default: from config)")
	flag.StringVar(&service, "s", "", "Download service (shorthand)")
	flag.StringVar(&configPath, "config", "", "Path to config.json (default: ~/.spotiflac/config.json)")
	flag.BoolVar(&saveLyrics, "lyrics", true, "Save .lrc lyrics file alongside FLAC")
	flag.BoolVar(&embedLyrics, "embed-lyrics", false, "Embed lyrics in audio file tags")
	flag.BoolVar(&showVersion, "version", false, "Show version")
	flag.BoolVar(&jsonOutput, "json", false, "Output results as JSON")

	flag.Usage = func() {
		fmt.Fprintf(os.Stderr, "Usage: spotiflac-cli [options] <spotify_url>\n\n")
		fmt.Fprintf(os.Stderr, "Download FLAC audio from a Spotify track, album, or playlist URL.\n\n")
		fmt.Fprintf(os.Stderr, "Options:\n")
		flag.PrintDefaults()
		fmt.Fprintf(os.Stderr, "\nExamples:\n")
		fmt.Fprintf(os.Stderr, "  spotiflac-cli https://open.spotify.com/track/...\n")
		fmt.Fprintf(os.Stderr, "  spotiflac-cli -o /mnt/music https://open.spotify.com/album/...\n")
		fmt.Fprintf(os.Stderr, "  spotiflac-cli -s qobuz https://open.spotify.com/track/...\n")
	}

	flag.Parse()

	if showVersion {
		fmt.Printf("spotiflac-cli %s\n", version)
		os.Exit(0)
	}

	if flag.NArg() < 1 {
		flag.Usage()
		os.Exit(1)
	}

	spotifyURL := flag.Arg(0)

	cfg, err := loadConfig(configPath)
	if err != nil {
		fmt.Fprintf(os.Stderr, "Error loading config: %v\n", err)
		os.Exit(1)
	}

	if service != "" {
		cfg.Downloader = service
	}
	if outputDir == "" {
		outputDir = cfg.DownloadPath
	}

	if err := backend.InitHistoryDB("SpotiFLAC"); err != nil {
		fmt.Fprintf(os.Stderr, "Warning: failed to init history DB: %v\n", err)
	}
	defer backend.CloseHistoryDB()

	// Fetch Spotify metadata
	fmt.Printf("Fetching metadata for: %s\n", spotifyURL)
	ctx, cancel := context.WithTimeout(context.Background(), 120*time.Second)
	defer cancel()

	data, err := backend.GetFilteredSpotifyData(ctx, spotifyURL, false, 0)
	if err != nil {
		fmt.Fprintf(os.Stderr, "Error fetching Spotify metadata: %v\n", err)
		os.Exit(1)
	}

	// Marshal and re-parse to determine type
	jsonData, err := json.Marshal(data)
	if err != nil {
		fmt.Fprintf(os.Stderr, "Error encoding metadata: %v\n", err)
		os.Exit(1)
	}

	type Result struct {
		File    string `json:"file"`
		Track   string `json:"track"`
		Artist  string `json:"artist"`
		Status  string `json:"status"`
		Error   string `json:"error,omitempty"`
	}

	var results []Result
	var succeeded, failed, skipped int

	// Try as track
	var trackResp backend.TrackResponse
	if err := json.Unmarshal(jsonData, &trackResp); err == nil && trackResp.Track.Name != "" {
		tr := trackResp.Track
		fmt.Printf("\nTrack: %s - %s\n", tr.Name, tr.Artists)
		fmt.Printf("Album: %s (%s)\n\n", tr.AlbumName, tr.ReleaseDate)

		trackOutputDir := buildFolderPath(outputDir, cfg, tr.AlbumArtist, tr.AlbumName, tr.ReleaseDate)

		filename, dlErr := downloadWithFallback(
			cfg, tr.SpotifyID,
			tr.Name, tr.Artists, tr.AlbumName, tr.AlbumArtist, tr.ReleaseDate, tr.Images,
			tr.TrackNumber, tr.DiscNumber, tr.TotalTracks, tr.TotalDiscs,
			tr.Copyright, tr.Publisher,
			trackOutputDir, tr.TrackNumber, saveLyrics, embedLyrics,
		)
		r := Result{Track: tr.Name, Artist: tr.Artists}
		if dlErr != nil {
			r.Status = "failed"
			r.Error = dlErr.Error()
			failed++
			fmt.Printf("  FAIL: %v\n", dlErr)
		} else {
			r.File = filename
			r.Status = "ok"
			succeeded++
			fmt.Printf("  OK: %s\n", filepath.Base(filename))
		}
		results = append(results, r)

	} else {
		// Try as album
		var albumResp backend.AlbumResponsePayload
		if err := json.Unmarshal(jsonData, &albumResp); err == nil && albumResp.AlbumInfo.Name != "" {
			info := albumResp.AlbumInfo
			fmt.Printf("\nAlbum: %s - %s\n", info.Name, info.Artists)
			fmt.Printf("Tracks: %d | Released: %s\n\n", info.TotalTracks, info.ReleaseDate)

			albumOutputDir := buildFolderPath(outputDir, cfg, info.Artists, info.Name, info.ReleaseDate)

			for i, tr := range albumResp.TrackList {
				fmt.Printf("[%d/%d] %s - %s\n", i+1, len(albumResp.TrackList), tr.Name, tr.Artists)

				position := tr.TrackNumber
				if position == 0 {
					position = i + 1
				}

				filename, dlErr := downloadWithFallback(
					cfg, tr.SpotifyID,
					tr.Name, tr.Artists, tr.AlbumName, tr.AlbumArtist, tr.ReleaseDate, tr.Images,
					tr.TrackNumber, tr.DiscNumber, tr.TotalTracks, tr.TotalDiscs,
					"", "",
					albumOutputDir, position, saveLyrics, embedLyrics,
				)
				r := Result{Track: tr.Name, Artist: tr.Artists}
				if dlErr != nil {
					r.Status = "failed"
					r.Error = dlErr.Error()
					failed++
					fmt.Printf("  FAIL: %v\n", dlErr)
				} else {
					r.File = filename
					r.Status = "ok"
					succeeded++
					fmt.Printf("  OK: %s\n", filepath.Base(filename))
				}
				results = append(results, r)

				// Small delay between tracks to be polite
				if i < len(albumResp.TrackList)-1 {
					time.Sleep(500 * time.Millisecond)
				}
			}

		} else {
			// Try as playlist
			var playlistResp backend.PlaylistResponsePayload
			if err := json.Unmarshal(jsonData, &playlistResp); err == nil && playlistResp.PlaylistInfo.Tracks.Total > 0 {
				info := playlistResp.PlaylistInfo
				fmt.Printf("\nPlaylist: %s (by %s)\n", info.Owner.Name, info.Owner.DisplayName)
				fmt.Printf("Tracks: %d\n\n", info.Tracks.Total)

				for i, tr := range playlistResp.TrackList {
					fmt.Printf("[%d/%d] %s - %s\n", i+1, len(playlistResp.TrackList), tr.Name, tr.Artists)

					// Each playlist track gets its own album folder
					trackAlbumArtist := tr.AlbumArtist
					if trackAlbumArtist == "" {
						trackAlbumArtist = tr.Artists
					}
					trackOutputDir := buildFolderPath(outputDir, cfg, trackAlbumArtist, tr.AlbumName, tr.ReleaseDate)

					filename, dlErr := downloadWithFallback(
						cfg, tr.SpotifyID,
						tr.Name, tr.Artists, tr.AlbumName, trackAlbumArtist, tr.ReleaseDate, tr.Images,
						tr.TrackNumber, tr.DiscNumber, tr.TotalTracks, tr.TotalDiscs,
						"", "",
						trackOutputDir, tr.TrackNumber, saveLyrics, embedLyrics,
					)
					r := Result{Track: tr.Name, Artist: tr.Artists}
					if dlErr != nil {
						r.Status = "failed"
						r.Error = dlErr.Error()
						failed++
						fmt.Printf("  FAIL: %v\n", dlErr)
					} else {
						r.File = filename
						r.Status = "ok"
						succeeded++
						fmt.Printf("  OK: %s\n", filepath.Base(filename))
					}
					results = append(results, r)

					if i < len(playlistResp.TrackList)-1 {
						time.Sleep(500 * time.Millisecond)
					}
				}
			} else {
				fmt.Fprintf(os.Stderr, "Error: could not parse Spotify response as track, album, or playlist\n")
				os.Exit(1)
			}
		}
	}

	// Summary
	total := succeeded + failed + skipped
	fmt.Printf("\nDone: %d/%d succeeded", succeeded, total)
	if failed > 0 {
		fmt.Printf(", %d failed", failed)
	}
	fmt.Println()

	if jsonOutput {
		out, _ := json.MarshalIndent(results, "", "  ")
		fmt.Println(string(out))
	}

	if failed > 0 {
		os.Exit(1)
	}
}
