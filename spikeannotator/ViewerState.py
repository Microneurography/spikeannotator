import sys
from dataclasses import dataclass, field
from turtle import update
from typing import Any, List, Optional, Union

import neo
import numpy as np
import quantities as pq
from neo import Event
from neo.io import NixIO
from PySide6.QtCore import QObject, Qt, Signal, Slot
from PySide6.QtWidgets import ( QFileDialog,QInputDialog)

from .APTrack_experiment_import import process_folder as open_aptrack
from .NeoOpenEphyisIO import open_ephys_to_neo

@dataclass
class tracked_neuron_unit:
    """
    a UI class for tracked neurons.
    This is to store the current state of neurons tracked

    #TODO: this abstraction is quite annoying. idx_arr in particular makes this hard to use
    """

    fibre_type: str = ""
    notes: str = ""
    idx_arr: List[int] = None
    event: Event = None

    def get_number_of_events(self):
        return len([x for x in self.idx_arr if x is not None])

    def get_window(self):
        # return the min-time & max-time of the events
        idx_arr_filt = [x[0] for x in self.idx_arr if x is not None]
        if len(idx_arr_filt) == 0:
            return None
        return (int(min(idx_arr_filt)), int(max(idx_arr_filt)))

    def get_idx_arr(self, event_signal, signal_chan):
        p = [None for x in range(len(event_signal))]
        for e in x.times:
            i = int(np.searchsorted(event_signal, e).base) - 1
            x = int(((e - event_signal[i]) * signal_chan.sampling_rate).base)
            p[i] = (
                x,
                signal_chan.base[event_signal[i].base * signal_chan.sampling_rate.base],
            )
        return p
    
    def get_latencies(self, event_signal):
        p = np.ones(len(event_signal)) * np.nan * pq.ms
        for e in self.event.times:
            i = np.searchsorted(event_signal, e) -1
            p[i] = (e-event_signal[i]).rescale(pq.ms)
        return p

        

def create_erp(signal_chan, idxs, offset=-1000, length=30000):
    arr = np.zeros((len(idxs), length))
    for i, x in enumerate(idxs):
        arr[i].flat[:] = signal_chan[int(x + offset) : int(x + offset + length)]
    return arr

class ViewerState(QObject):
    onLoadNewFile = Signal()
    onUnitChange = Signal(int)
    onUnitGroupChange = Signal()
    onStimNoChange = Signal(int)

    def __init__(self, parent=None, segment=None, spike_groups=None):
        super().__init__(parent)

        self.segment: neo.Segment = segment
        self.spike_groups: List[tracked_neuron_unit] = spike_groups
        self.stimno = 0
        self.cur_spike_group = 0
        self.trace_count = 3
        self.analog_signal_erp = None
        self.analog_signal: neo.AnalogSignal = None
        self.sampling_rate = None
        self.event_signal: neo.Event = None

    @Slot(int)
    def setUnitGroup(self, unitid: int):
        self.cur_spike_group = unitid
        self.onUnitGroupChange.emit()

    @Slot(int, int)
    def setUnit(self, latency):
        evt = self.spike_groups[self.cur_spike_group].event

        if len(evt) > 0:
            to_keep = (evt < self.event_signal[self.stimno]) | (
                (evt
                > self.event_signal[
                    self.stimno + 1  
                ]
            ) if self.stimno < len(self.event_signal)-1 else False)  # Prevent crash on final index
            # TODO: we should remove by index, and also use purely the timestamps not idx_arr
            self.spike_groups[self.cur_spike_group].event = evt[to_keep]
            evt = evt[to_keep]
        if latency is None:
            self.spike_groups[self.cur_spike_group].idx_arr[self.stimno] = None

        else:
            self.spike_groups[self.cur_spike_group].idx_arr[self.stimno] = (
                latency,
                1,
            )  # TODO: fix

            self.spike_groups[self.cur_spike_group].event = evt.merge(
                Event(
                    ((latency / self.analog_signal.sampling_rate).rescale(pq.s))
                    + self.event_signal[[self.stimno]],
                )
            )

        self.spike_groups[self.cur_spike_group].event.sort()

        self.onUnitChange.emit(self.stimno)

    @Slot(Event)
    def updateUnit(self, event):
        self.spike_groups[self.cur_spike_group].event = event
        del self.spike_groups[self.cur_spike_group].idx_arr
        self.update_idx_arrs()
        self.onUnitGroupChange.emit()

    @Slot(int)
    def setStimNo(self, stimno: int):
        self.stimno = max(min(stimno, self.analog_signal_erp.shape[0]-1), 0)
        self.onStimNoChange.emit(self.stimno)

    @Slot(str, str)
    def loadFile(self, fname, type="h5", **kwargs):
        data, analog_signal, event_signal, spike_groups = load_file(fname, type)
        self.segment = data
        self.cur_spike_group = 0
        self.set_data(analog_signal, event_signal, spike_groups=spike_groups)

    @Slot()
    def addUnitGroup(self):
        self.spike_groups.append(tracked_neuron_unit(event=Event()))
        self.update_idx_arrs()
        self.onUnitGroupChange.emit()

    def update_idx_arrs(self):
        for sg in self.spike_groups:
            #if sg.idx_arr is not None:
            #    continue
            p = [None for x in range(len(self.event_signal))]
            for e in sg.event.times:
                i = int(np.searchsorted(self.event_signal, e, side="right").base) - 1
                x = int(
                    ((e - self.event_signal[i]) * self.analog_signal.sampling_rate).base
                )
                
                p[i] = (x, self.analog_signal[self.analog_signal.time_index(e)][0])

            sg.idx_arr = p

    def set_data(
        self,
        analog_signal=None,
        event_signal=None,
        other_signals=None,
        spike_groups=None,
    ):
        if analog_signal is not None:
            self.analog_signal = analog_signal
        if event_signal is not None:
            self.event_signal = event_signal
        if spike_groups is not None:
            self.spike_groups = spike_groups
            

        # ensure essentials are populated on state
        if self.spike_groups is None:
            spike_groups = [tracked_neuron_unit(event=Event())]
        if self.analog_signal is None:
            analog_signal = neo.AnalogSignal([], pq.mV, sampling_rate=1 * pq.Hz)
        if self.event_signal is None:
            event_signal = neo.Event()

        s = int(np.array(self.analog_signal.sampling_rate.base) // 2)  # 500ms
        self.analog_signal_erp = create_erp(
            self.analog_signal.rescale("mV").as_array()[:, 0],
            (self.event_signal.as_array() - self.analog_signal.t_start.base)
            * np.array(self.analog_signal.sampling_rate, dtype=int),
            0,
            s,
        )
        self.sampling_rate = int(np.array(self.analog_signal.sampling_rate.base))
        if len(self.spike_groups) == 0:
            self.spike_groups.append(tracked_neuron_unit(event=Event()))

        self.update_idx_arrs()
        self.onLoadNewFile.emit()

    def getUnitGroup(self, unit=None):
        if unit is None:
            unit = self.cur_spike_group
        return self.spike_groups[unit]

    def set_segment(self,data:neo.Segment):

        kwargs = {}
        cur_evt= [i for i,x in enumerate(data.events) if id(x)== id(self.event_signal)]
        if len(cur_evt) == 0:
            event_signal = data.events[0]
            kwargs['event_signal'] =event_signal
        else:
            event_signal = data.events[cur_evt[0]]

        cur_analogsig = [i for i,x in enumerate(data.analogsignals) if id(x)== id(self.analog_signal)]
        if len(cur_analogsig) == 0:
            analog_signal = data.analogsignals[0]
            kwargs['analog_signal'] = analog_signal
        else:
            analog_signal = data.analogsignals[cur_evt[0]]

        spike_event_groups = []

        for x in data.events:
            if not x.name.startswith("unit"):
                continue

            spike_event_groups.append(tracked_neuron_unit(event=x)) 
        
        cur_sel = [i for i,x in enumerate(spike_event_groups) if id(x.event)== id(self.spike_groups[self.cur_spike_group])]
        if len(cur_sel) == 0:
            self.cur_spike_group = 0
        else:
            self.cur_spike_group = self.cur_sel[0]
        
        self.segment = data

        self.set_data(analog_signal, event_signal, spike_groups=spike_event_groups)

def load_file(data_path, type="h5", **kwargs):
    if type == "h5":
        data = NixIO(data_path, mode="ro").read_block(0).segments[0]
    # elif type == "dabsys":
        #data = import_dapsys_csv_files(data_path)[0].segments[0]
    elif type == "openEphys":
        data = open_ephys_to_neo(data_path)
    
    elif type == "matlab":
        data = open_matlab_to_neo(data_path)

    elif type == "APTrack":
        data = open_aptrack(data_path)
        # blk = neo.OpenEphysIO(data_path).read_block(0)
        # data = blk.segments[0]
    if len(data.events) == 0:
        event_signal = Event()
    else:
        event_signal = data.events[0]
    signal_chan = data.analogsignals[0]

    spike_event_groups = []

    for x in data.events:
        if not x.name.startswith("unit"):
            continue
        # info = x.times[:,np.newaxis]-event_signal
        # p = [None for x in range(len(event_signal))]

        spike_event_groups.append(tracked_neuron_unit(event=x))

    return data, signal_chan, event_signal, spike_event_groups

def prompt_for_neo_file(type):
    if type is None:
        type = QInputDialog().getItem(
            None,
            "Select file type",
            "Filetype",
            ["h5", "openEphys", "APTrack", "matlab"],
        )[0]

    if type == "h5":
        fname = QFileDialog.getOpenFileName(None, "Open")[0]
    elif type in ("openEphys","APTrack","matlab"):
        fname = QFileDialog.getExistingDirectory(None, "Open OpenEphys")
    else:
        raise Exception(f"Unknown filetype: {type}")
    return fname, type

def open_matlab_to_neo(folder):
    from scipy.io import loadmat
    from pathlib import Path
    matfiles = Path(folder).glob("*.mat")
    seg = neo.Segment()
    for m in matfiles:
        mf = loadmat(m)
        asig = neo.AnalogSignal(mf['data'].T, pq.V, sampling_rate = mf['samplerate'][0,0] *pq.Hz, name=m.stem)
        seg.analogsignals.append(asig)
    return seg
