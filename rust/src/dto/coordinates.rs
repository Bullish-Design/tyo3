/// 1-based position in a file.  Serialized to Python dict via pythonize.
#[derive(Debug, Clone, PartialEq, Eq, Hash, serde::Serialize, serde::Deserialize)]
pub struct PositionDto {
    /// 1-based line number
    pub line: u32,
    /// 1-based column (Unicode codepoints)
    pub column: u32,
}

/// A range between two positions.  Serialized to Python dict via pythonize.
#[derive(Debug, Clone, PartialEq, Eq, Hash, serde::Serialize, serde::Deserialize)]
pub struct RangeDto {
    pub start: PositionDto,
    pub end: PositionDto,
}

/// A range within a specific file.  Serialized to Python dict via pythonize.
#[derive(Debug, Clone, PartialEq, Eq, Hash, serde::Serialize, serde::Deserialize)]
pub struct FileRangeDto {
    pub path: String,
    pub range: RangeDto,
}
