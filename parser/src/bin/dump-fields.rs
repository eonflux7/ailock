//! Print every field (path name = value) of the first pawn and controller seen after `at_tick`.
//! Dev tool for finding field names; usage: dump-fields <demo> [at_tick]
use std::collections::HashSet;
use std::fs::File;
use std::io::BufReader;

use anyhow::{Context as _, Result};
use haste::demofile::DemoFile;
use haste::entities::{DeltaHeader, Entity};
use haste::parser::{Context, Parser, Visitor};

struct Dump {
    at_tick: i32,
    seen: HashSet<String>,
}

fn path_name(entity: &Entity, key: &u64) -> Option<String> {
    let path = entity.get_path(key)?;
    let mut names = Vec::new();
    let mut field = entity.serializer().get_child(path.get(0)?);
    names.push(field?.var_name.str.to_string());
    for i in path.iter().skip(1) {
        field = field.and_then(|f| f.get_child(*i as usize));
        match field {
            Some(f) => names.push(f.var_name.str.to_string()),
            None => names.push(i.to_string()),
        }
    }
    Some(names.join("."))
}

impl Visitor for Dump {
    fn on_entity(&mut self, ctx: &Context, _h: DeltaHeader, entity: &Entity) -> Result<()> {
        if ctx.tick() < self.at_tick {
            return Ok(());
        }
        let name = entity.serializer().serializer_name.str.to_string();
        let wanted = ["CCitadelPlayerPawn", "CCitadelPlayerController", "CCitadelGameRulesProxy"];
        if !wanted.contains(&name.as_str()) || !self.seen.insert(name.clone()) {
            return Ok(());
        }
        println!("== {name} (entity {})", entity.index());
        let mut rows: Vec<String> = entity
            .iter()
            .map(|(k, v)| format!("{} = {:?}", path_name(entity, k).unwrap_or_default(), v))
            .collect();
        rows.sort();
        for r in rows {
            println!("{r}");
        }
        Ok(())
    }
}

fn main() -> Result<()> {
    let args: Vec<String> = std::env::args().collect();
    let path = args.get(1).context("usage: dump-fields <demo> [at_tick]")?;
    let at_tick = args.get(2).map(|s| s.parse()).transpose()?.unwrap_or(20000);
    let demo = DemoFile::start_reading(BufReader::new(File::open(path)?))?;
    let mut parser = Parser::from_stream_with_visitor(demo, Dump { at_tick, seen: HashSet::new() })?;
    parser.run_to_end()
}
