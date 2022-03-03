// import { useState, useEffect } from "react";
import React, { useCallback, useRef, useState } from "react"
import { render } from "react-dom"
import Excalidraw, { serializeAsJSON } from "@excalidraw/excalidraw"
import { AppState, ExcalidrawImperativeAPI, LibraryItems } from "@excalidraw/excalidraw/types/types"
import { ConfigProps, ConnectionStates } from "./types"

import "./style.css"
import { getJsonScript, getLocalStorageJson, setLocalStorageJson } from "./utils"
import { CollabAPI } from "./collab/collaboration"
import { useEventListener } from "./hooks/useEventListener"
import { reconcileElements } from "./collab/reconciliation"
import { ImportedDataState } from "@excalidraw/excalidraw/types/data/types"

window.React = React

const defaultConfig: ConfigProps = {
  BROADCAST_RESOLUTION: 150,
  ELEMENT_UPDATES_BEFORE_FULL_RESYNC: 50,
  INITIAL_DATA: [],
  LANGUAGE_CODE: "en-US",
  SAVE_ROOM_INTERVAL: 15000,
  SOCKET_URL: "",
  USER_NAME: "",
}

const config: ConfigProps = { ...defaultConfig, ...getJsonScript("excalidraw-config") }
const msg: Record<string, string> = { ...getJsonScript("custom-messages") }

let params = new URLSearchParams(window.location.search.slice(1))

// let hash = new URLSearchParams(window.location.hash.slice(1))

// function updateHashParams(name: string, value: string) {
//   hash.set(name, value)
//   window.location.hash = hash.toString()
// }

function saveLibrary(items: LibraryItems) {
  localStorage.setItem("_library", JSON.stringify(items))
}

function loadLibrary(): LibraryItems {
  return JSON.parse(localStorage.getItem("_library") ?? "[]")
}

let localData: ImportedDataState = JSON.parse(localStorage.getItem(params.get("room")!) ?? "{}")

let importedAppState = Object.assign(
  { editingElement: null, resizingElement: null, draggingElement: null },
  localData?.appState ?? {}
)

let initialData = {
  elements: reconcileElements(localData?.elements ?? [], config.INITIAL_DATA, importedAppState),
  appState: importedAppState,
  libraryItems: loadLibrary(),
}

let collabAPI = new CollabAPI(config)

const _addLibraries = "_addLibraries"

function IndexPage() {
  let draw = useRef<ExcalidrawImperativeAPI>(null)
  let [connectionState, setConnectionState] = useState<ConnectionStates>(collabAPI.connectionState)
  collabAPI.excalidrawApiRef = draw
  collabAPI.connectionStateSetter = setConnectionState
  window.draw = draw

  const saveStateToLocalStorage = useCallback(() => {
    // if an element is deleted and the user closes the tab before it can sync to the
    // server, the deleted element will be restored on reload, because we do not save
    // deleted elements. is this a problem? how correct do we have to be here?
    const elements = draw.current?.getSceneElements() ?? []
    const appState: Partial<AppState> = { ...draw.current?.getAppState() }
    delete appState.collaborators
    localStorage.setItem(params.get("room")!, serializeAsJSON(elements, appState))
  }, [draw])

  const loadEnqueuedLibraries = useCallback(() => {
    let urls: string[] = getLocalStorageJson(_addLibraries, [])
    if (draw.current) {
      for (let url of urls) {
        draw.current.importLibrary(url)
      }
      setLocalStorageJson(_addLibraries, [])
    }
  }, [draw])

  useEventListener("blur", saveStateToLocalStorage, window)
  useEventListener("focus", loadEnqueuedLibraries, window)
  useEventListener("hashchange", saveStateToLocalStorage, window)
  useEventListener("beforeunload", saveStateToLocalStorage, window)
  useEventListener("visibilitychange", saveStateToLocalStorage, document)

  // FIXME: adding items to the libraray via an addLink fails. see issue #5
  // https://gitlab.tba-hosting.de/lpa-aflek-alice/excalidraw-lti-application/-/issues/5

  return connectionState == "CONNECTED" ? (
    <Excalidraw
      ref={draw}
      initialData={initialData}
      onPointerUpdate={collabAPI.broadcastCursorMovement}
      onChange={collabAPI.broadcastElements}
      UIOptions={{
        canvasActions: {
          loadScene: false,
          clearCanvas: false,
        },
      }}
      autoFocus={true}
      handleKeyboardGlobally={true}
      langCode={config.LANGUAGE_CODE}
      onLibraryChange={saveLibrary}
      libraryReturnUrl={config.LIBRARY_RETURN_URL}
    />
  ) : (
    <p style={{ margin: "1rem" }}>{msg.NOT_LOGGED_IN}</p>
  )
}

render(<IndexPage />, document.getElementById("app"))
