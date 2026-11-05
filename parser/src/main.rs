//! Per-player state from a Deadlock demo, sampled once per game second (configurable).
//!
//! usage: ailock-demo <demo> [samples_per_second]
//! stdout CSV: game_time_s,account_id,hero_id,team,health,max_health,alive,x,y,z
//!
//! Game time follows haste's deadlock-gametime example: net tick minus paused ticks, times the tick
//! interval, minus the game rules' start time. Positions come from the hero pawn the controller points to.
use std::collections::HashMap;
use std::fs::File;
use std::io::{BufReader, BufWriter, Write};

use anyhow::{Context as _, Result};
use haste::demofile::DemoFile;
use haste::demostream::CmdHeader;
use haste::entities::{deadlock_coord_from_cell, ehandle_to_index, fkey_from_path, DeltaHeader, Entity};
use haste::fieldvalue::{FieldValue, FieldValueConversionError};
use haste::fxhash;
use haste::parser::{Context, Parser, Visitor};
use haste::valveprotos::common::{CnetMsgTick, EDemoCommands, NetMessages};
use haste::valveprotos::prost::Message;

const GAMERULES: u64 = fxhash::hash_bytes(b"CCitadelGameRulesProxy");
const CONTROLLER: u64 = fxhash::hash_bytes(b"CCitadelPlayerController");
const PAWN: u64 = fxhash::hash_bytes(b"CCitadelPlayerPawn");
const STEAM_ID64_BASE: u64 = 76561197960265728;

macro_rules! key {
    ($($p:literal),+) => { const { fkey_from_path(&[$($p),+]) } };
}

#[derive(Default, Clone, Copy)]
struct Player {
    account_id: u64,
    hero_id: u64,
    team: u64,
    health: i64,
    max_health: i64,
    pawn: i32,
}

#[derive(Default, Clone, Copy)]
struct Pawn {
    pos: [f32; 3],
    alive: bool,
}

struct Extract<W: Write> {
    out: W,
    step: f32,
    next_sample: f32,
    net_tick: u32,
    tick_interval: Option<f32>,
    game_start: Option<f32>,
    paused_ticks: i32,
    players: HashMap<i32, Player>,
    pawns: HashMap<i32, Pawn>,
}

/// embedded structs (m_PlayerDataGlobal) may be keyed with or without their parent name
fn get_either<T>(e: &Entity, nested: &u64, flat: &u64) -> Option<T>
where
    FieldValue: TryInto<T, Error = FieldValueConversionError>,
{
    e.get_value(nested).or_else(|| e.get_value(flat))
}

fn coord(e: &Entity, cell: &u64, vec: &u64) -> Option<f32> {
    Some(deadlock_coord_from_cell(e.get_value(cell)?, e.get_value(vec)?))
}

impl<W: Write> Extract<W> {
    fn game_time(&self) -> Option<f32> {
        Some((self.net_tick as f32 - self.paused_ticks as f32) * self.tick_interval? - self.game_start?)
    }
}

impl<W: Write> Visitor for Extract<W> {
    fn on_cmd(&mut self, ctx: &Context, header: &CmdHeader, _data: &[u8]) -> Result<()> {
        if self.tick_interval.is_none() && header.cmd == EDemoCommands::DemSyncTick {
            self.tick_interval = Some(ctx.tick_interval());
        }
        Ok(())
    }

    fn on_packet(&mut self, _ctx: &Context, packet_type: u32, data: &[u8]) -> Result<()> {
        if packet_type == NetMessages::NetTick as u32 {
            if let Some(t) = CnetMsgTick::decode(data)?.tick {
                self.net_tick = t;
            }
        }
        Ok(())
    }

    fn on_entity(&mut self, _ctx: &Context, _h: DeltaHeader, e: &Entity) -> Result<()> {
        if e.serializer_name_heq(GAMERULES) {
            let start: f32 = e.get_value(&key!("m_pGameRules", "m_flGameStartTime")).unwrap_or(0.0);
            if start > 0.001 {
                self.game_start = Some(start);
                self.paused_ticks = e.get_value(&key!("m_pGameRules", "m_nTotalPausedTicks")).unwrap_or(0);
            }
        } else if e.serializer_name_heq(CONTROLLER) {
            let steam: u64 = e.get_value(&key!("m_steamID")).unwrap_or(0);
            if steam < STEAM_ID64_BASE {
                return Ok(()); // bots / spectators / casters
            }
            let pawn: u64 = e.get_value(&key!("m_hHeroPawn")).unwrap_or(0);
            self.players.insert(e.index(), Player {
                account_id: steam - STEAM_ID64_BASE,
                hero_id: get_either(e, &key!("m_PlayerDataGlobal", "m_nHeroID"), &key!("m_nHeroID")).unwrap_or(0),
                team: e.get_value(&key!("m_iTeamNum")).unwrap_or(0),
                health: get_either(e, &key!("m_PlayerDataGlobal", "m_iHealth"), &key!("m_iHealth")).unwrap_or(0),
                max_health: get_either(e, &key!("m_PlayerDataGlobal", "m_iHealthMax"), &key!("m_iHealthMax")).unwrap_or(0),
                pawn: ehandle_to_index(pawn as u32),
            });
        } else if e.serializer_name_heq(PAWN) {
            let pos = (|| {
                Some([
                    coord(e, &key!("CBodyComponent", "m_skeletonInstance", "m_vecOrigin", "m_cellX"),
                          &key!("CBodyComponent", "m_skeletonInstance", "m_vecOrigin", "m_vecX"))?,
                    coord(e, &key!("CBodyComponent", "m_skeletonInstance", "m_vecOrigin", "m_cellY"),
                          &key!("CBodyComponent", "m_skeletonInstance", "m_vecOrigin", "m_vecY"))?,
                    coord(e, &key!("CBodyComponent", "m_skeletonInstance", "m_vecOrigin", "m_cellZ"),
                          &key!("CBodyComponent", "m_skeletonInstance", "m_vecOrigin", "m_vecZ"))?,
                ])
            })();
            if let Some(pos) = pos {
                let life: u64 = e.get_value(&key!("m_lifeState")).unwrap_or(0);
                self.pawns.insert(e.index(), Pawn { pos, alive: life == 0 });
            }
        }
        Ok(())
    }

    fn on_tick_end(&mut self, _ctx: &Context) -> Result<()> {
        let Some(t) = self.game_time() else { return Ok(()) };
        if t < self.next_sample {
            return Ok(());
        }
        // sample on the grid even if a tick was skipped
        self.next_sample = ((t / self.step).floor() + 1.0) * self.step;
        let sample = (t / self.step).floor() * self.step;
        for p in self.players.values() {
            let Some(pawn) = self.pawns.get(&p.pawn) else { continue };
            writeln!(
                self.out,
                "{:.2},{},{},{},{},{},{},{:.1},{:.1},{:.1}",
                sample, p.account_id, p.hero_id, p.team, p.health, p.max_health, pawn.alive as u8,
                pawn.pos[0], pawn.pos[1], pawn.pos[2]
            )?;
        }
        Ok(())
    }
}

fn main() -> Result<()> {
    let args: Vec<String> = std::env::args().collect();
    let path = args.get(1).context("usage: ailock-demo <demo> [samples_per_second]")?;
    let hz: f32 = args.get(2).map(|s| s.parse()).transpose()?.unwrap_or(1.0);
    let mut out = BufWriter::new(std::io::stdout().lock());
    writeln!(out, "game_time_s,account_id,hero_id,team,health,max_health,alive,x,y,z")?;
    let demo = DemoFile::start_reading(BufReader::new(File::open(path)?))?;
    let visitor = Extract {
        out, step: 1.0 / hz, next_sample: 0.0, net_tick: 0, tick_interval: None, game_start: None,
        paused_ticks: 0, players: HashMap::new(), pawns: HashMap::new(),
    };
    let mut parser = Parser::from_stream_with_visitor(demo, visitor)?;
    parser.run_to_end()
}
