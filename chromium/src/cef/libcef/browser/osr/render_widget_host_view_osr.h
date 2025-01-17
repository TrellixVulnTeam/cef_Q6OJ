// Copyright (c) 2014 The Chromium Embedded Framework Authors.
// Portions copyright (c) 2012 The Chromium Authors. All rights reserved.
// Use of this source code is governed by a BSD-style license that can be
// found in the LICENSE file.

#ifndef CEF_LIBCEF_BROWSER_OSR_RENDER_WIDGET_HOST_VIEW_OSR_H_
#define CEF_LIBCEF_BROWSER_OSR_RENDER_WIDGET_HOST_VIEW_OSR_H_
#pragma once

#include <set>
#include <vector>

#include "include/cef_base.h"
#include "include/cef_browser.h"

#include "base/memory/weak_ptr.h"
#include "build/build_config.h"
#include "content/browser/renderer_host/delegated_frame_host.h"
#include "content/browser/renderer_host/render_widget_host_view_base.h"
#include "ui/compositor/compositor.h"

#if defined(OS_LINUX)
#include "ui/base/x/x11_util.h"
#endif

#if defined(OS_MACOSX)
#include "content/browser/renderer_host/browser_compositor_view_mac.h"
#endif

#if defined(OS_WIN)
#include "ui/gfx/win/window_impl.h"
#endif

namespace content {
class RenderWidgetHost;
class RenderWidgetHostImpl;
class RenderWidgetHostViewGuest;
class BackingStore;
}

class CefBeginFrameTimer;
class CefBrowserHostImpl;
class CefCopyFrameGenerator;
class CefSoftwareOutputDeviceOSR;
class CefWebContentsViewOSR;

#if defined(OS_MACOSX)
#ifdef __OBJC__
@class CALayer;
@class NSWindow;
#else
class CALayer;
class NSWindow;
#endif
#endif

#if defined(USE_X11)
class CefWindowX11;
#endif

///////////////////////////////////////////////////////////////////////////////
// CefRenderWidgetHostViewOSR
//
//  An object representing the "View" of a rendered web page. This object is
//  responsible for sending paint events to the the CefRenderHandler
//  when window rendering is disabled. It is the implementation of the
//  RenderWidgetHostView that the cross-platform RenderWidgetHost object uses
//  to display the data.
//
//  Comment excerpted from render_widget_host.h:
//
//    "The lifetime of the RenderWidgetHostView is tied to the render process.
//     If the render process dies, the RenderWidgetHostView goes away and all
//     references to it must become NULL."
//
// RenderWidgetHostView class hierarchy described in render_widget_host_view.h.
///////////////////////////////////////////////////////////////////////////////

#if defined(OS_MACOSX)
class MacHelper;
#endif

class CefRenderWidgetHostViewOSR
    : public content::RenderWidgetHostViewBase,
      public ui::CompositorDelegate
#if !defined(OS_MACOSX)
      , public content::DelegatedFrameHostClient
#endif
{
 public:
  CefRenderWidgetHostViewOSR(const bool transparent,
                             content::RenderWidgetHost* widget,
                             CefRenderWidgetHostViewOSR* parent_host_view);
  ~CefRenderWidgetHostViewOSR() override;

  // RenderWidgetHostView implementation.
  void InitAsChild(gfx::NativeView parent_view) override;
  content::RenderWidgetHost* GetRenderWidgetHost() const override;
  void SetSize(const gfx::Size& size) override;
  void SetBounds(const gfx::Rect& rect) override;
  gfx::Vector2dF GetLastScrollOffset() const override;
  gfx::NativeView GetNativeView() const override;
  gfx::NativeViewAccessible GetNativeViewAccessible() override;
  void Focus() override;
  bool HasFocus() const override;
  bool IsSurfaceAvailableForCopy() const override;
  void Show() override;
  void Hide() override;
  bool IsShowing() override;
  gfx::Rect GetViewBounds() const override;
  void SetBackgroundColor(SkColor color) override;
  bool LockMouse() override;
  void UnlockMouse() override;

#if defined(OS_MACOSX)
  ui::AcceleratedWidgetMac* GetAcceleratedWidgetMac() const override;
  void SetActive(bool active) override;
  void ShowDefinitionForSelection() override;
  bool SupportsSpeech() const override;
  void SpeakSelection() override;
  bool IsSpeaking() const override;
  void StopSpeaking() override;
#endif  // defined(OS_MACOSX)

  // RenderWidgetHostViewBase implementation.
  void OnSwapCompositorFrame(uint32_t output_surface_id,
                             cc::CompositorFrame frame) override;
  void ClearCompositorFrame() override;
  void InitAsPopup(content::RenderWidgetHostView* parent_host_view,
                   const gfx::Rect& pos) override;
  void InitAsFullscreen(
      content::RenderWidgetHostView* reference_host_view) override;
  void UpdateCursor(const content::WebCursor& cursor) override;
  void SetIsLoading(bool is_loading) override;
  void RenderProcessGone(base::TerminationStatus status,
                         int error_code) override;
  void Destroy() override;
  void SetTooltipText(const base::string16& tooltip_text) override;

  gfx::Size GetRequestedRendererSize() const override;
  gfx::Size GetPhysicalBackingSize() const override;
  void CopyFromCompositingSurface(
      const gfx::Rect& src_subrect,
      const gfx::Size& dst_size,
      const content::ReadbackRequestCallback& callback,
      const SkColorType color_type) override;
  void CopyFromCompositingSurfaceToVideoFrame(
      const gfx::Rect& src_subrect,
      const scoped_refptr<media::VideoFrame>& target,
      const base::Callback<void(const gfx::Rect&, bool)>& callback) override;
  bool CanCopyToVideoFrame() const override;
  void BeginFrameSubscription(
      std::unique_ptr<content::RenderWidgetHostViewFrameSubscriber> subscriber)
      override;
  void EndFrameSubscription() override;
  bool HasAcceleratedSurface(const gfx::Size& desired_size) override;
  gfx::Rect GetBoundsInRootWindow() override;
  content::BrowserAccessibilityManager*
      CreateBrowserAccessibilityManager(
          content::BrowserAccessibilityDelegate* delegate,
          bool for_root_frame) override;
  void LockCompositingSurface() override;
  void UnlockCompositingSurface() override;

#if defined(TOOLKIT_VIEWS) || defined(USE_AURA)
  void ShowDisambiguationPopup(const gfx::Rect& rect_pixels,
                               const SkBitmap& zoomed_bitmap) override;
#endif
  void ImeCompositionRangeChanged(
      const gfx::Range& range,
      const std::vector<gfx::Rect>& character_bounds) override;

  void SetNeedsBeginFrames(bool enabled) override;

  // ui::CompositorDelegate implementation.
  std::unique_ptr<cc::SoftwareOutputDevice> CreateSoftwareOutputDevice(
      ui::Compositor* compositor) override;

#if !defined(OS_MACOSX)
  // DelegatedFrameHostClient implementation.
  ui::Layer* DelegatedFrameHostGetLayer() const override;
  bool DelegatedFrameHostIsVisible() const override;
  SkColor DelegatedFrameHostGetGutterColor(SkColor color) const override;
  gfx::Size DelegatedFrameHostDesiredSizeInDIP() const override;
 bool DelegatedFrameCanCreateResizeLock() const override;
  std::unique_ptr<content::ResizeLock> DelegatedFrameHostCreateResizeLock(
      bool defer_compositor_lock) override;
  void DelegatedFrameHostResizeLockWasReleased() override;
  void DelegatedFrameHostSendReclaimCompositorResources(
      int output_surface_id,
      bool is_swap_ack,
      const cc::ReturnedResourceArray& resources) override;
  void SetBeginFrameSource(cc::BeginFrameSource* source) override;
  bool IsAutoResizeEnabled() const override;
#endif  // !defined(OS_MACOSX)

  bool InstallTransparency();

  void WasResized();
  void GetScreenInfo(content::ScreenInfo* results);
  void OnScreenInfoChanged();
  void Invalidate(CefBrowserHost::PaintElementType type);
  void SendKeyEvent(const content::NativeWebKeyboardEvent& event);
  void SendMouseEvent(const blink::WebMouseEvent& event);
  void SendMouseWheelEvent(const blink::WebMouseWheelEvent& event);
  void SendFocusEvent(bool focus);
  void UpdateFrameRate();

  void HoldResize();
  void ReleaseResize();

  void OnPaint(const gfx::Rect& damage_rect,
               int bitmap_width,
               int bitmap_height,
               void* bitmap_pixels);

  bool IsPopupWidget() const {
    return popup_type_ != blink::WebPopupTypeNone;
  }

  void ImeSetComposition(
      const CefString& text,
      const std::vector<CefCompositionUnderline>& underlines,
      const CefRange& replacement_range,
      const CefRange& selection_range);
  void ImeCommitText(const CefString& text,
                     const CefRange& replacement_range,
                     int relative_cursor_pos);
  void ImeFinishComposingText(bool keep_selection);
  void ImeCancelComposition();

  void AddGuestHostView(CefRenderWidgetHostViewOSR* guest_host);
  void RemoveGuestHostView(CefRenderWidgetHostViewOSR* guest_host);

  // Register a callback that will be executed when |guest_host_view| receives
  // OnSwapCompositorFrame. The callback triggers repaint of the embedder view.
  void RegisterGuestViewFrameSwappedCallback(
      content::RenderWidgetHostViewGuest* guest_host_view);

  CefRefPtr<CefBrowserHostImpl> browser_impl() const { return browser_impl_; }
  void set_browser_impl(CefRefPtr<CefBrowserHostImpl> browser) {
    browser_impl_ = browser;
  }

  void set_popup_host_view(CefRenderWidgetHostViewOSR* popup_view) {
    popup_host_view_ = popup_view;
  }
  void set_child_host_view(CefRenderWidgetHostViewOSR* popup_view) {
    child_host_view_ = popup_view;
  }

  ui::Compositor* GetCompositor() const;
  content::RenderWidgetHostImpl* render_widget_host() const
      { return render_widget_host_; }
  ui::Layer* GetRootLayer() const;

 private:
  content::DelegatedFrameHost* GetDelegatedFrameHost() const;

  void SetFrameRate();
  void SetDeviceScaleFactor();
  void ResizeRootLayer();

  // Called by CefBeginFrameTimer to send a BeginFrame request.
  void OnBeginFrameTimerTick();
  void SendBeginFrame(base::TimeTicks frame_time,
                      base::TimeDelta vsync_period);

  void CancelWidget();

  void OnScrollOffsetChanged();

  void OnGuestViewFrameSwapped(
      content::RenderWidgetHostViewGuest* guest_host_view);

  void InvalidateInternal(const gfx::Rect& bounds_in_pixels);

  void RequestImeCompositionUpdate(bool start_monitoring);

#if defined(OS_MACOSX)
  friend class MacHelper;
#endif
  void PlatformCreateCompositorWidget();
  void PlatformResizeCompositorWidget(const gfx::Size& size);
  void PlatformDestroyCompositorWidget();

#if defined(USE_AURA)
  ui::PlatformCursor GetPlatformCursor(blink::WebCursorInfo::Type type);
#endif

  const bool transparent_;

  float scale_factor_;
  int frame_rate_threshold_ms_;

#if !defined(OS_MACOSX)
  std::unique_ptr<ui::Compositor> compositor_;
  gfx::AcceleratedWidget compositor_widget_;
  std::unique_ptr<content::DelegatedFrameHost> delegated_frame_host_;
  std::unique_ptr<ui::Layer> root_layer_;
#endif

#if defined(OS_WIN)
  std::unique_ptr<gfx::WindowImpl> window_;
#elif defined(OS_MACOSX)
  NSWindow* window_;
  CALayer* background_layer_;
  std::unique_ptr<content::BrowserCompositorMac> browser_compositor_;
  MacHelper* mac_helper_;
#elif defined(USE_X11)
  CefWindowX11* window_;
  std::unique_ptr<ui::XScopedCursor> invisible_cursor_;
#endif

  // Used to control the VSync rate in subprocesses when BeginFrame scheduling
  // is enabled.
  std::unique_ptr<CefBeginFrameTimer> begin_frame_timer_;

  // Used for direct rendering from the compositor when GPU compositing is
  // disabled. This object is owned by the compositor.
  CefSoftwareOutputDeviceOSR* software_output_device_;

  // Used for managing copy requests when GPU compositing is enabled.
  std::unique_ptr<CefCopyFrameGenerator> copy_frame_generator_;

  bool hold_resize_;
  bool pending_resize_;

  // The associated Model.  While |this| is being Destroyed,
  // |render_widget_host_| is NULL and the message loop is run one last time
  // Message handlers must check for a NULL |render_widget_host_|.
  content::RenderWidgetHostImpl* render_widget_host_;

  bool has_parent_;
  CefRenderWidgetHostViewOSR* parent_host_view_;
  CefRenderWidgetHostViewOSR* popup_host_view_;
  CefRenderWidgetHostViewOSR* child_host_view_;
  std::set<CefRenderWidgetHostViewOSR*> guest_host_views_;

  CefRefPtr<CefBrowserHostImpl> browser_impl_;

  bool is_showing_;
  bool is_destroyed_;
  gfx::Rect popup_position_;

  // The last scroll offset of the view.
  gfx::Vector2dF last_scroll_offset_;
  bool is_scroll_offset_changed_pending_;

  base::WeakPtrFactory<CefRenderWidgetHostViewOSR> weak_ptr_factory_;

  DISALLOW_COPY_AND_ASSIGN(CefRenderWidgetHostViewOSR);
};

#endif  // CEF_LIBCEF_BROWSER_OSR_RENDER_WIDGET_HOST_VIEW_OSR_H_

